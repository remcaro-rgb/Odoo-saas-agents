"""Composition root + GitHub Actions entry point (Tier 1 — make it runnable).

The orchestrator library (Phases A–F) is driven by `handle_webhook`, but until
now nothing built the real object graph or fed it a GitHub event. This module is
that missing layer — it turns the tested library into a runnable agent.

A GitHub Actions workflow (see `deploy/workflows/`) runs `python -m
agents.implementation` on a repo event. GitHub hands the workflow the event name
and a JSON payload file via `$GITHUB_EVENT_NAME` / `$GITHUB_EVENT_PATH`. `run()`
reads them, consults the `Rollout` canary gate, and — unless the gate says SKIP —
builds the real clients (`OpenCodeClient`, `GitWorkspace`, `GhCliClient`) and
calls `handle_webhook`. The SHADOW stage wraps the GitHub client so the agent
runs for real but posts nothing.

Exit code: 0 for a clean run — a correct escalation included — and 1 only for a
missing-config abort or an unhandled error, so a GitHub Action shows red on a
genuine failure, not on a routine escalation.
"""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .core import FlowResult, IterationResult, Orchestrator, feature_name
from .gate1 import Gate1, SubprocessCheckRunner
from .git_workspace import GitWorkspace
from .github_io import GhCliClient, GitHubClient, ShadowGitHubClient, handle_webhook
from .notifier import FakeNotifier, Notifier, SlackNotifier, notify_escalation
from .observability import EventLog
from .opencode_client import DEFAULT_BASE_URL, OpenCodeClient
from .pushback import push_implementation
from .rollout import Rollout, RolloutDecision
from .speckit_driver import SpecKitDriver


@dataclass(frozen=True)
class AgentConfig:
    """The deploy-time configuration the composition root reads from the env."""

    opencode_base_url: str
    opencode_password: str | None
    data_plane_repo: str | None
    workspace_root: str
    gate1_enabled: bool
    # "lint" (default — `ruff check` only, no Odoo runtime needed in the
    # Action) or "full" (lint + build + tests — reserved for the future
    # agentlab-SSH runner; see TIER-1-RUNBOOK.md §7).
    gate1_check_set: str
    slack_webhook_url: str | None
    # `GH_TOKEN` is the implementation-bot App's installation token, minted in
    # the workflow by actions/create-github-app-token@v1. It pushes the
    # implementation back to the PR head branch; the App's GH-side identity
    # comes through automatically (commits show as `implementation-bot[bot]`).
    bot_token: str | None
    # The App's numeric id, used to construct the canonical noreply email
    # `<id>+implementation-bot[bot]@users.noreply.github.com`.
    app_id: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AgentConfig:
        """Build an `AgentConfig` from environment variables.

        `DATA_PLANE_REPO` is the `owner/name` slug of the repo spec PRs open
        against. `GATE1_ENABLED` stays off until the agentlab Odoo build
        environment exists (a Tier-2 seam); until then the coder runs the
        deterministic Odoo-rule checks only. `SLACK_WEBHOOK_URL` enables Slack
        notifications on escalation; unset = no Slack (a `FakeNotifier`).
        `GH_TOKEN` + `IMPLEMENTATION_BOT_APP_ID` drive the implement→PR-branch
        push; unset = no push (the agent runs but its work stays local).
        """
        return cls(
            opencode_base_url=env.get("OPENCODE_BASE_URL") or DEFAULT_BASE_URL,
            opencode_password=env.get("OPENCODE_SERVER_PASSWORD") or None,
            data_plane_repo=env.get("DATA_PLANE_REPO") or None,
            workspace_root=env.get("WORKSPACE_ROOT") or ".",
            gate1_enabled=env.get("GATE1_ENABLED", "false").strip().lower() == "true",
            gate1_check_set=(env.get("GATE1_CHECK_SET") or "lint").strip().lower(),
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL") or None,
            bot_token=env.get("GH_TOKEN") or None,
            app_id=env.get("IMPLEMENTATION_BOT_APP_ID") or None,
        )


def load_event(env: Mapping[str, str]) -> tuple[str, dict[str, Any]] | None:
    """Read the GitHub event GitHub Actions handed the workflow.

    Returns `(event_name, payload)` from `$GITHUB_EVENT_NAME` and the JSON file
    at `$GITHUB_EVENT_PATH`, or `None` when either is unset (not running inside a
    GitHub Action — there is nothing to do). A set-but-unreadable
    `$GITHUB_EVENT_PATH` is a real misconfiguration and is left to raise.
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

    The PR number (as a string) when the event carries one — a labeled
    `pull_request` or an `issue_comment` — else the pushed branch, else the
    repo's full name. The FIXTURES / OPT_IN stages match this id against their
    configured `ROLLOUT_FIXTURES` / `ROLLOUT_OPT_IN` sets; SHADOW and DEFAULT_ON
    decide the same for every target, so the exact id does not matter there.
    """
    pull_request = payload.get("pull_request") or {}
    if pull_request.get("number") is not None:
        return str(pull_request["number"])
    issue = payload.get("issue") or {}
    if issue.get("number") is not None:
        return str(issue["number"])
    ref = str(payload.get("ref") or "")
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    repository = payload.get("repository") or {}
    return str(repository.get("full_name") or "unknown")


def build_opencode_client(config: AgentConfig) -> OpenCodeClient:
    """Construct the HTTP client for the headless OpenCode service."""
    return OpenCodeClient(
        base_url=config.opencode_base_url, password=config.opencode_password
    )


def build_orchestrator(
    config: AgentConfig,
    client: OpenCodeClient,
    *,
    decision: RolloutDecision = RolloutDecision.SHADOW,
) -> Orchestrator:
    """Wire the orchestrator object graph over an OpenCode client.

    Gate 1 (the build / lint / test gate) is wired only when `GATE1_ENABLED` is
    set: it needs the agentlab Odoo environment, a later (Tier-2) seam. Until
    then `gate1` is `None` and the coder runs Odoo-rule validation only.

    `decision` defaults to `SHADOW` — if a caller forgets to thread the rollout
    decision through, the resulting orchestrator still suppresses the
    container's autonomous push (the safer default; ACT must be explicit).
    """
    driver = SpecKitDriver(client)
    workspace = GitWorkspace(config.workspace_root)
    gate1: Gate1 | None
    if config.gate1_enabled:
        runner = SubprocessCheckRunner(cwd=config.workspace_root)
        if config.gate1_check_set == "full":
            # `full` -> the build + tests checks reach for `odoo` + a Postgres,
            # which only the future agentlab-SSH runner can provide (§7).
            gate1 = Gate1(runner)
        else:
            # `lint` (the default) runs `ruff check {addon}` only — safe in a
            # vanilla GitHub-Actions runner, no Odoo runtime required.
            gate1 = Gate1(runner, checks=(("lint", "ruff check {addon}"),))
    else:
        gate1 = None
    return Orchestrator(
        workspace, driver,
        repo=config.data_plane_repo,
        gate1=gate1,
        shadow=decision is RolloutDecision.SHADOW,
    )


def build_notifier(config: AgentConfig, decision: RolloutDecision) -> Notifier:
    """The escalation notifier for a rollout decision.

    ACT with a configured `SLACK_WEBHOOK_URL` -> a real `SlackNotifier`. SHADOW
    is always a `FakeNotifier` — Slack is a world-facing side effect, so SHADOW
    suppresses it just like comments / labels. ACT without a URL is also a
    `FakeNotifier` (the agent must not crash because Slack isn't configured).
    """
    if decision is RolloutDecision.SHADOW:
        return FakeNotifier()
    if config.slack_webhook_url:
        return SlackNotifier(config.slack_webhook_url)
    return FakeNotifier()


def notify_outcome(
    notifier: Notifier,
    result: FlowResult | IterationResult | None,
    *,
    pr: int | None,
) -> None:
    """Page on-call when an agent run escalates; no-op for a clean outcome.

    A push-event escalation has no PR number (`pr is None`); skip Slack rather
    than emit an unlinkable page. Clean (`implemented`, `iterated`,
    `acknowledged`, `ignored`) outcomes route to the PR comment alone — Slack
    is reserved for the "needs-human" cases.
    """
    if pr is None or result is None or getattr(result, "status", "") != "escalated":
        return
    if isinstance(result, FlowResult):
        reason = f"implement flow escalated at the {result.stage} stage"
    else:  # IterationResult
        reason = f"reporter iteration ({result.intent.value}) escalated"
    notify_escalation(notifier, pr, reason)


def _extract_pr(event_name: str, payload: Mapping[str, Any]) -> int | None:
    """The PR number an event carries (used to route the Slack page); `None`
    for events that do not target a single PR (e.g. a `push`)."""
    if event_name == "issue_comment":
        number = (payload.get("issue") or {}).get("number")
    elif event_name == "pull_request":
        number = (payload.get("pull_request") or {}).get("number")
    else:
        number = None
    return int(number) if isinstance(number, int) else None


def build_github(repo: str, decision: RolloutDecision) -> GitHubClient:
    """The GitHub write client for a rollout decision.

    ACT -> a real `GhCliClient` that posts and labels. SHADOW -> that client
    wrapped so reads stay real but comments / labels are only recorded.

    Only the GitHub API client is shadowed: a SHADOW run still drives OpenCode
    and provisions a real checkout, so it produces a faithful draft. There is no
    `git push` to suppress today — the implement->PR-branch push is unwired
    (docs/TIER-1-RUNBOOK.md §7); when wired it must consult the rollout decision.
    """
    real = GhCliClient(repo)
    if decision is RolloutDecision.SHADOW:
        return ShadowGitHubClient(real)
    return real


def run(env: Mapping[str, str] | None = None, *, log: EventLog | None = None) -> int:
    """Load the GitHub event, consult the rollout gate, and run the agent.

    Returns a process exit code: 0 for a clean run (a correct escalation
    included), 1 for a missing-config abort or an unhandled error.
    """
    env = os.environ if env is None else env
    log = log or EventLog(run_id=env.get("GITHUB_RUN_ID"))

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
    try:
        orchestrator = build_orchestrator(config, client, decision=decision)
        github = build_github(config.data_plane_repo, decision)
        result = handle_webhook(event_name, payload, orchestrator, github)
        # Push the implementation back to the PR head branch (Tier 2). The
        # `pushback.push_implementation` call is shadow-aware — in SHADOW it
        # records the would-push and returns. The push needs the App token +
        # repo + a result that carries the session id and branch.
        _maybe_push(config, client, decision, result, log)
    except Exception as exc:
        traceback.print_exc()
        log.emit("error", detail=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        client.close()

    log.emit(
        "outcome",
        status=result.status if result is not None else "none",
        shadow=decision is RolloutDecision.SHADOW,
    )
    notify_outcome(
        build_notifier(config, decision),
        result,
        pr=_extract_pr(event_name, payload),
    )
    return 0


def _maybe_push(
    config: AgentConfig,
    client: OpenCodeClient,
    decision: RolloutDecision,
    result: FlowResult | IterationResult | None,
    log: EventLog,
) -> None:
    """Drive `pushback.push_implementation` when the outcome produced code.

    A no-op unless the agent has a token + repo + a session-bearing
    successful result. The pushback module is itself shadow-aware.
    """
    if not (config.bot_token and config.data_plane_repo):
        return
    if result is None:
        return
    if getattr(result, "status", "") not in ("implemented", "iterated"):
        return
    session_id = getattr(result, "session_id", None)
    branch = getattr(result, "branch", None)
    if not (session_id and branch):
        return
    push_url = (
        f"https://x-access-token:{config.bot_token}"
        f"@github.com/{config.data_plane_repo}.git"
    )
    push_implementation(
        workspace_root=config.workspace_root,
        oc_client=client,
        session_id=session_id,
        branch=branch,
        push_url=push_url,
        feature=feature_name(branch),
        decision=decision,
        log=log,
        app_id=config.app_id,
    )


def main() -> int:
    """`python -m agents.implementation` entry point."""
    return run(os.environ)
