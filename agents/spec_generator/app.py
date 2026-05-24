"""Composition root + GitHub Actions entry point (Tier 1).

A GitHub Actions workflow (see ``deploy/workflows/spec-generator.yml``) runs
``python -m agents.spec_generator`` on an issue webhook. GitHub hands the
workflow the event name and a JSON payload file via ``$GITHUB_EVENT_NAME`` /
``$GITHUB_EVENT_PATH``. ``run()`` reads them, consults the ``Rollout`` canary
gate (reused from the implementation agent — same wire shape), and — unless
the gate says SKIP — builds the real clients and calls ``handle_webhook``.

Exit code: 0 for a clean run (a sensitive-content escalation counts as clean),
1 only for a missing-config abort or an unhandled error so a GitHub Action
shows red on a real failure, not on a routine refuse-to-draft.

Tier 1 deliberately does NOT push to a branch or open a PR — the drafter
records the would-be spec content, the shadow client records the would-be
comment + labels, and the structured event log makes both auditable. Tier 2
wires the bot token and pushes for real.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agents.implementation.notifier import (
    FakeNotifier,
    Notifier,
    SlackNotifier,
    notify_escalation,
)
from agents.implementation.observability import EventLog
from agents.implementation.opencode_client import DEFAULT_BASE_URL, OpenCodeClient
from agents.implementation.rollout import Rollout, RolloutDecision

from .axiom_sink import maybe_build_axiom_sink
from .core import DraftResult, Orchestrator
from .github_io import (
    GhCliIssueClient,
    IssueClient,
    ShadowIssueClient,
    handle_webhook,
)
from .pushback import push_spec
from .run_store import (
    PHASE_ESCALATED,
    PHASE_INTENT_CONFIRMED,
    DraftRecord,
    RunStore,
    build_run_store,
)
from .session_store import JsonFileSessionStore, SessionStore

# `EventLog` tags every record with its `agent` field — overridden via the
# module global in `observability`. We set it here so spec-gen records are
# distinguishable from impl-agent records in the Better Stack drain.
SPEC_GEN_AGENT_TAG = "spec_generator"


@dataclass(frozen=True)
class AgentConfig:
    """The deploy-time configuration the composition root reads from the env."""

    opencode_base_url: str
    opencode_password: str | None
    data_plane_repo: str | None
    workspace_root: str
    slack_webhook_url: str | None
    # The spec-generator-bot App's installation token + numeric id. Tier 1
    # SHADOW mode does not push — these are optional and only consumed once
    # Tier 2 wires the workspace push. They are surfaced here now so the
    # workflow contract is forward-stable.
    bot_token: str | None
    app_id: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AgentConfig:
        """Build an `AgentConfig` from environment variables.

        `DATA_PLANE_REPO` is the `owner/name` slug of the repo the spec PR
        opens against (same repo as the implementation agent — that is the
        whole point of the handoff). `SPEC_GENERATOR_BOT_APP_ID` +
        `GH_TOKEN` drive future writes; unset = SHADOW-friendly (Tier 1).
        """
        return cls(
            opencode_base_url=env.get("OPENCODE_BASE_URL") or DEFAULT_BASE_URL,
            opencode_password=env.get("OPENCODE_SERVER_PASSWORD") or None,
            data_plane_repo=env.get("DATA_PLANE_REPO") or None,
            workspace_root=env.get("WORKSPACE_ROOT") or ".",
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL") or None,
            bot_token=env.get("GH_TOKEN") or None,
            app_id=env.get("SPEC_GENERATOR_BOT_APP_ID") or None,
        )


def load_event(env: Mapping[str, str]) -> tuple[str, dict[str, Any]] | None:
    """Read the GitHub event the workflow handed us.

    Returns `(event_name, payload)` or `None` when either is unset (not
    running inside a GitHub Action — nothing to do).
    """
    event_name = env.get("GITHUB_EVENT_NAME")
    event_path = env.get("GITHUB_EVENT_PATH")
    if not event_name or not event_path:
        return None
    with open(event_path, encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return event_name, payload


def rollout_target(event_name: str, payload: Mapping[str, Any]) -> str:
    """The stable target id the `Rollout` canary gate decides on.

    For issue webhooks: the issue number (as a string). For issue comments
    on a PR: the PR number. Falls back to the repo's full name for events
    that carry neither.
    """
    issue = payload.get("issue") or {}
    if issue.get("number") is not None:
        return str(issue["number"])
    pull_request = payload.get("pull_request") or {}
    if pull_request.get("number") is not None:
        return str(pull_request["number"])
    repository = payload.get("repository") or {}
    return str(repository.get("full_name") or "unknown")


def build_opencode_client(config: AgentConfig) -> OpenCodeClient:
    return OpenCodeClient(
        base_url=config.opencode_base_url, password=config.opencode_password
    )


def build_orchestrator(
    config: AgentConfig,
    client: OpenCodeClient,
    *,
    decision: RolloutDecision = RolloutDecision.SHADOW,
) -> Orchestrator:
    """Wire the spec-generator orchestrator over an OpenCode client."""
    return Orchestrator(
        oc_client=client, shadow=decision is RolloutDecision.SHADOW
    )


def build_notifier(config: AgentConfig, decision: RolloutDecision) -> Notifier:
    """The escalation notifier for a rollout decision.

    SHADOW + ACT-without-webhook URL -> `FakeNotifier`. Slack is the
    world-facing side effect, so SHADOW suppresses it like every other write.
    """
    if decision is RolloutDecision.SHADOW:
        return FakeNotifier()
    if config.slack_webhook_url:
        return SlackNotifier(config.slack_webhook_url)
    return FakeNotifier()


def build_github(repo: str, decision: RolloutDecision) -> IssueClient:
    """The GitHub write client for a rollout decision.

    ACT -> a real `GhCliIssueClient`. SHADOW -> that client wrapped so reads
    stay real but comments / labels are only recorded.
    """
    real = GhCliIssueClient(repo)
    if decision is RolloutDecision.SHADOW:
        return ShadowIssueClient(real)
    return real


def _extract_pr_or_issue(payload: Mapping[str, Any]) -> int | None:
    """Used as the Slack page anchor — the issue (or PR) at the center of the run."""
    issue = payload.get("issue") or {}
    if issue.get("number") is not None:
        return int(issue["number"])
    pull_request = payload.get("pull_request") or {}
    if pull_request.get("number") is not None:
        return int(pull_request["number"])
    return None


def notify_outcome(
    notifier: Notifier, result: DraftResult | None, *, anchor: int | None
) -> None:
    """Page on-call for an escalation; no-op for a clean draft/skip."""
    if anchor is None or result is None or result.status != "escalated":
        return
    reason = (
        result.skip_reason.value if result.skip_reason is not None else "unknown"
    )
    notify_escalation(notifier, anchor, f"spec-generator escalated: {reason}")


def _log_shadow_record(log: EventLog, github: IssueClient) -> None:
    """If the client is the shadow wrapper, dump what *would* have been posted.

    This is the audit trail SHADOW exists for — the structured-JSON event
    list of would-be writes is exactly the proof the runbook asks for.
    """
    if not isinstance(github, ShadowIssueClient):
        return
    for issue, body in github.issue_comments_posted:
        log.emit(
            "shadow-write",
            kind="issue_comment",
            issue=issue,
            body_preview=body[:200],
        )
    for pr, body in github.pr_comments_posted:
        log.emit(
            "shadow-write",
            kind="pr_comment",
            pr=pr,
            body_preview=body[:200],
        )
    for issue, label in github.issue_labels_added:
        log.emit("shadow-write", kind="issue_label", issue=issue, label=label)
    for pr, label in github.pr_labels_added:
        log.emit("shadow-write", kind="pr_label", pr=pr, label=label)


def run(env: Mapping[str, str] | None = None, *, log: EventLog | None = None) -> int:
    """Load the GitHub event, consult the rollout gate, and run the agent."""
    env = os.environ if env is None else env
    # The implementation agent's EventLog tags records with `agent="implementation"`
    # via its module-global; we override it here so spec-gen records are
    # distinguishable. (A future shared package would make this a constructor
    # arg — for Tier 1 the in-place override is fine.)
    #
    # Observability sink: stdout always, plus Axiom when AXIOM_TOKEN +
    # AXIOM_DATASET env vars are set. Unset = stdout-only. The composite
    # sink is the same shape `EventLog` already expects; Axiom flushes at
    # the end of the run.
    axiom_sink = maybe_build_axiom_sink(env)
    log = log or EventLog(
        run_id=env.get("GITHUB_RUN_ID"),
        sink=axiom_sink,
    )
    _tag_spec_generator(log)

    loaded = load_event(env)
    if loaded is None:
        log.emit("no-event", detail="not running inside a GitHub Action")
        return 0
    event_name, payload = loaded

    rollout = Rollout.from_env(env)
    target = rollout_target(event_name, payload)
    decision = rollout.decide(target)
    log.emit(
        "rollout-decision",
        webhook=event_name,
        stage=str(rollout.stage),
        decision=str(decision),
        target=target,
    )
    if decision is RolloutDecision.SKIP:
        log.emit("skipped", reason="rollout gate decided SKIP")
        return 0

    config = AgentConfig.from_env(env)
    if config.data_plane_repo is None:
        log.emit("misconfigured", detail="DATA_PLANE_REPO is required to run")
        return 1

    client = build_opencode_client(config)
    result: DraftResult | Any | None = None
    # Wall-clock at the start of the agent's "real work" — used by Tier 6's
    # `Median time issue -> draft PR (ms)` panel. We measure from after the
    # rollout gate (so SHADOW + ACT runs are comparable) up to the outcome
    # emit, which is the latest moment that's still "agent did the work";
    # the push happens *after* outcome and is reported separately.
    handle_start = time.monotonic()
    try:
        orchestrator = build_orchestrator(config, client, decision=decision)
        github = build_github(config.data_plane_repo, decision)
        sessions = build_session_store(config)
        run_store = build_run_store_from_env(config, sessions, env)
        result = handle_webhook(
            event_name, payload, orchestrator, github,
            session_store=run_store,
        )
        duration_ms = int((time.monotonic() - handle_start) * 1000)
        if isinstance(result, DraftResult):
            log.emit(
                "outcome",
                status=result.status,
                issue=result.issue,
                skip_reason=(
                    result.skip_reason.value if result.skip_reason is not None else None
                ),
                shadow=decision is RolloutDecision.SHADOW,
                duration_ms=duration_ms,
            )
            for note in result.notes:
                log.emit("note", text=note)
            # Tier 3 state writes: every successful draft inserts a row, every
            # escalation marks the existing row (if any) as escalated. SHADOW
            # skips state writes since nothing else is real either.
            if decision is not RolloutDecision.SHADOW:
                _record_outcome_state(run_store, result, log)
            # Tier 2 push-back: write the spec, push the branch, open the PR.
            pr_number = _maybe_push_spec(config, decision, result, log)
            if (
                decision is not RolloutDecision.SHADOW
                and pr_number is not None
                and result.issue is not None
            ):
                run_store.record_pr_opened(result.issue, pr_number)
        elif result is not None:
            log.emit(
                "iteration",
                intent=getattr(result.intent, "value", str(result.intent)),
                status=result.status,
                shadow=decision is RolloutDecision.SHADOW,
                duration_ms=duration_ms,
            )
            for note in result.notes:
                log.emit("note", text=note)
            if decision is not RolloutDecision.SHADOW:
                _record_iteration_state(run_store, result, payload)
        _log_shadow_record(log, github)
    except Exception as exc:
        traceback.print_exc()
        log.emit("error", detail=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        client.close()

    notify_outcome(
        build_notifier(config, decision),
        result if isinstance(result, DraftResult) else None,
        anchor=_extract_pr_or_issue(payload),
    )

    # Flush the Axiom buffer last — after all the run's records are emitted.
    # Failures are best-effort (logged to stderr by the sink itself); the
    # stdout audit trail is the authoritative copy regardless.
    sink = getattr(log, "_sink", None)
    axiom = getattr(sink, "axiom", None)
    if axiom is not None:
        axiom.flush()

    return 0


def _tag_spec_generator(log: EventLog) -> None:
    """Re-tag a borrowed `EventLog` so records are filtered as spec-gen.

    The implementation agent's `EventLog` hardcodes `AGENT = "implementation"`.
    We don't want to fork the class for one string — instead, every spec-gen
    record gets a sentinel field `agent=spec_generator` that overrides the
    default at sink time. The Better Stack drain keys on this field.
    """
    original_emit = log.emit

    def emit_with_agent_override(event: str, **fields: Any) -> dict[str, Any]:
        fields.setdefault("agent_override", SPEC_GEN_AGENT_TAG)
        return original_emit(event, **fields)

    log.emit = emit_with_agent_override  # type: ignore[method-assign]


def build_session_store(config: AgentConfig) -> SessionStore:
    """The Tier-2 JSON-file fallback for session-id storage.

    Tier 3's `PostgresRunStore` is the primary store; this fallback is
    still wired so a transient Postgres outage doesn't break the
    iterate workflow (the run store delegates `get/set/delete` here on
    DB error).
    """
    path = os.path.join(config.workspace_root, ".spec-generator-sessions.json")
    return JsonFileSessionStore(path)


def build_run_store_from_env(
    config: AgentConfig, sessions: SessionStore, env: Mapping[str, str]
) -> RunStore:
    """The Tier-3 phase-transition writer.

    Reads `CONTROL_PLANE_PG_DSN` from `env`. Unset → `NoOpRunStore` (the
    agent still works but writes nothing to `spec_generator_runs`).
    """
    # Mapping[str, str] -> plain dict for the helper.
    return build_run_store(dict(env), sessions=sessions)


def _maybe_push_spec(
    config: AgentConfig,
    decision: RolloutDecision,
    result: DraftResult,
    log: EventLog,
) -> int | None:
    """Push the drafted spec + open the PR — Tier 2's ACT path.

    Returns the PR number when one was opened (or already existed for the
    spec branch); `None` when the push was skipped or short-circuited.

    A no-op unless:
      - The drafter produced a non-empty body.
      - The agent has a bot token (Tier 2 GitHub App provisioned).
      - The rollout decision is ACT.

    SHADOW is handled inside ``push_spec`` itself (it logs the shadow-push
    intent and returns), so a SHADOW run still emits the audit record.
    """
    if result.drafted is None or not result.drafted.body.strip():
        return None
    if not (config.bot_token and config.data_plane_repo):
        log.emit(
            "push-skipped",
            reason="no bot token — Tier 2 App not provisioned yet",
        )
        return None
    push_url = (
        f"https://x-access-token:{config.bot_token}"
        f"@github.com/{config.data_plane_repo}.git"
    )
    return push_spec(
        workspace_root=config.workspace_root,
        spec_path=result.drafted.path,
        spec_body=result.drafted.body,
        branch=result.drafted.branch,
        push_url=push_url,
        issue=result.drafted.issue,
        title=_intake_title_from_result(result),
        decision=decision,
        log=log,
        app_id=config.app_id,
    )


def _record_outcome_state(
    run_store: RunStore, result: DraftResult, log: EventLog
) -> None:
    """Persist the orchestrator's draft outcome into ``spec_generator_runs``.

    Drafted -> INSERT (UPSERT on the unique issue_number index) so re-runs
    on the same issue UPDATE in place. Escalated -> existing-row UPDATE to
    `phase=escalated` only when a row already exists; we don't INSERT a
    blank row for an issue we never drafted against.
    """
    if result.issue is None:
        return
    if result.status == "drafted" and result.drafted is not None:
        kind_str = (
            result.kind.kind.value if result.kind is not None else "unknown"
        )
        confidence = (
            float(result.kind.confidence) if result.kind is not None else 0.0
        )
        run_store.record_draft(
            DraftRecord(
                issue=result.drafted.issue,
                kind=kind_str,
                confidence=confidence,
                branch=result.drafted.branch,
                spec_path=result.drafted.path,
                opencode_session_id=result.drafted.session_id,
                metadata={
                    "open_questions": len(result.drafted.open_questions),
                    "captured_items": len(result.drafted.captured_items),
                },
            )
        )
        log.emit("state-recorded", issue=result.issue, phase="drafted")
    elif result.status == "escalated":
        run_store.record_phase(result.issue, PHASE_ESCALATED)
        log.emit("state-recorded", issue=result.issue, phase="escalated")


def _record_iteration_state(
    run_store: RunStore, outcome: Any, payload: Mapping[str, Any]
) -> None:
    """Persist refiner outcomes (Tier 2 reporter Q&A).

    The orchestrator emits an IterationOutcome with `status` in {handed_off,
    clarified, reclassified, ignored}. `handed_off` advances to
    `intent_confirmed` (the handoff to the Implementation Agent). Every
    refiner run bumps `last_reporter_activity_at` so the sweep's silence
    timer resets.
    """
    issue_number = (payload.get("issue") or {}).get("number")
    if issue_number is None:
        return
    issue = int(issue_number)
    # Every refiner run = reporter activity (the comment came from a human).
    run_store.record_reporter_activity(issue)
    status = getattr(outcome, "status", "")
    if status == "handed_off":
        run_store.record_phase(issue, PHASE_INTENT_CONFIRMED)


def _intake_title_from_result(result: DraftResult) -> str:
    """Best-effort title for the PR — falls back to a generic if unknown."""
    if result.drafted is None:
        return f"draft spec for issue #{result.issue}"
    # The drafter's `path` ends with `<slug>-design.md`; the slug captures
    # the title well enough for a PR title without re-threading the intake.
    name = result.drafted.path.rsplit("/", 1)[-1]
    return name.removesuffix("-design.md").replace("-", " ")


def main() -> int:
    """`python -m agents.spec_generator` entry point."""
    return run(os.environ)
