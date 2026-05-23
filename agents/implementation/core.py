"""The orchestrator core (Phase B).

Routes a normalized `Event` to a flow, and runs the **planning half** of the
implement flow: map the spec to Spec-Kit form, drive `/plan` -> `/tasks` ->
`/analyze`, escalate on incoherence, and commit the planning artifacts.

Fix-briefs take a fast-path that skips `/plan` + `/tasks` (plan decision: the
Spec-Kit pipeline is ~10x heavier than needed for a tiny fix).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .classifier import Classifier, CommentIntent, HeuristicClassifier
from .coder import Coder, ImplementResult
from .commenter import escalation_notice, iteration_update
from .events import Event, EventType, SpecKind
from .gate1 import Gate1
from .provisioning import provision_workspace
from .spec_mapper import detect_spec_kind, to_speckit
from .speckit_driver import SpecKitDriver
from .workspace import Workspace

_FLOWS = {
    EventType.INTENT_CONFIRMED: "implement",
    EventType.ISSUE_COMMENT: "reporter_iteration",
    EventType.HUMAN_PUSH: "human_commit",
    EventType.CRON_SWEEP: "sweep",
}


@dataclass
class PlanningResult:
    """Outcome of the planning half — what the implement flow should do next."""

    status: str                       # "ready_to_implement" | "escalated"
    feature: str
    fast_path: bool = False
    session_id: str | None = None
    findings: list[str] = field(default_factory=list)


@dataclass
class FlowResult:
    """Outcome of the full implement flow (planning + coding)."""

    status: str                          # "implemented" | "escalated"
    stage: str                           # "planning" | "implement"
    planning: PlanningResult
    implement: ImplementResult | None = None
    # The OpenCode session id + PR branch — surfaced so the composition root
    # can drive the implement→PR-branch push (`pushback.push_implementation`).
    session_id: str | None = None
    branch: str | None = None


@dataclass
class IterationResult:
    """Outcome of handling one reporter comment (Phase D)."""

    intent: CommentIntent
    status: str               # "iterated" | "acknowledged" | "escalated" | "ignored"
    comment: str = ""         # the GitHub reply to post ("" = post nothing)
    session_id: str | None = None
    branch: str | None = None  # surfaced for `pushback.push_implementation`


def route(event: Event) -> str:
    """Map an event to its flow name. Only `implement` is built in Phase B."""
    return _FLOWS.get(event.type, "unsupported")


def feature_name(branch: str | None) -> str:
    """Derive a Spec-Kit feature name from a branch (`agent/spec-1500` -> `spec-1500`)."""
    return (branch or "").rsplit("/", 1)[-1] or "feature"


# The first `custom-addons/<name>` path a spec mentions is the addon it targets.
_ADDON_RE = re.compile(r"\bcustom-addons/([A-Za-z0-9_]+)")


def addon_prefix_from_spec(spec_text: str, feature: str) -> str:
    """Derive the addon prefix a spec targets.

    Returns ``custom-addons/<name>/`` for the *first* ``custom-addons/<name>``
    path the spec mentions. When the spec names no addon, falls back to the
    feature name (``custom-addons/<feature>/``) so the prefix is always
    deterministic — a webhook caller can omit it and let the orchestrator
    resolve it.

    "First mention" relies on the project's design-spec convention that the
    `Scope of work` line (near the top of `_TEMPLATE-design.md`) names the
    target addon before any dependency addon. A spec that mentions a dependency
    addon first would mis-resolve — keep the target addon's path first.
    """
    match = _ADDON_RE.search(spec_text or "")
    name = match.group(1) if match else feature
    return f"custom-addons/{name}/"


_SESSION_PATH = "specs/{feature}/.agent-session"


@dataclass(frozen=True)
class SessionRecord:
    """What `reporter_iteration` needs to resume work on a feature's PR."""

    session_id: str
    addon_prefix: str


def save_session(workspace: Workspace, feature: str, record: SessionRecord) -> None:
    """Persist a feature's OpenCode session id + addon prefix, so a later reporter
    comment can re-enter the same session and re-validate the same addon.

    Written into the feature's branch so it survives the separate triggers a PR
    sees over its life; committing it with the branch is Phase-D infra wiring.
    """
    payload = json.dumps(
        {"session_id": record.session_id, "addon_prefix": record.addon_prefix}
    )
    workspace.write(_SESSION_PATH.format(feature=feature), payload + "\n")


def load_session(workspace: Workspace, feature: str) -> SessionRecord | None:
    """Return the saved `SessionRecord` for a feature, or None if absent/unreadable."""
    path = _SESSION_PATH.format(feature=feature)
    if not workspace.exists(path):
        return None
    try:
        data = json.loads(workspace.read(path))
        return SessionRecord(data["session_id"], data["addon_prefix"])
    except (ValueError, KeyError, TypeError):
        return None


class Orchestrator:
    """Drives the planning half of the implement flow over a Workspace + driver."""

    def __init__(
        self,
        workspace: Workspace,
        driver: SpecKitDriver,
        coder: Coder | None = None,
        classifier: Classifier | None = None,
        repo: str | None = None,
        gate1: Gate1 | None = None,
    ) -> None:
        self.workspace = workspace
        self.driver = driver
        self.coder = coder
        self.classifier: Classifier = classifier or HeuristicClassifier()
        self.repo = repo
        self.gate1 = gate1

    def _provision(self, session_id: str, branch: str) -> None:
        """Check the agent branch out into the OpenCode session's workspace, when
        a data-plane repo is configured (a no-op otherwise — e.g. in unit tests
        and shadow mode)."""
        if self.repo and branch:
            provision_workspace(self.driver.client, session_id, self.repo, branch)

    def run_planning(self, event: Event) -> PlanningResult:
        self.workspace.checkout(event.branch or "")
        feature = feature_name(event.branch)
        spec_text = self.workspace.read(event.spec_path or "")
        kind = detect_spec_kind(event.spec_path or "", spec_text)

        # Fix-brief fast-path — skip the heavyweight /plan + /tasks pipeline.
        if kind is SpecKind.FIX:
            return PlanningResult(
                status="ready_to_implement", feature=feature, fast_path=True
            )

        # Design spec — full Spec-Kit planning pipeline.
        spec_md = f"specs/{feature}/spec.md"
        self.workspace.write(spec_md, to_speckit(spec_text))

        session = self.driver.client.create_session(title=f"impl/{feature}")
        self.driver.run_plan(session.id)
        self.driver.run_tasks(session.id)
        analysis = self.driver.run_analyze(session.id)

        if not analysis.coherent:
            self.workspace.escalate(
                "spec-refinement-needed", "; ".join(analysis.findings)
            )
            return PlanningResult(
                status="escalated",
                feature=feature,
                session_id=session.id,
                findings=analysis.findings,
            )

        # Commit the planning artifacts that exist (OpenCode's /plan and /tasks
        # write plan.md / tasks.md into the shared workspace).
        artifacts = [spec_md, f"specs/{feature}/plan.md", f"specs/{feature}/tasks.md"]
        present = [path for path in artifacts if self.workspace.exists(path)]
        self.workspace.commit(present, f"[impl-agent] plan: {feature}")
        return PlanningResult(
            status="ready_to_implement", feature=feature, session_id=session.id
        )

    def implement(self, event: Event, addon_prefix: str | None = None) -> FlowResult:
        """The full implement flow: planning, then hand off to the coder loop.

        Planning and coding share one OpenCode session — a design spec's session
        is reused; a fix-brief (which skipped planning) gets one created here.

        ``addon_prefix`` may be omitted: it is then derived from the spec via
        `addon_prefix_from_spec`, so a webhook caller need not resolve it.
        """
        planning = self.run_planning(event)
        if planning.status == "escalated":
            return FlowResult(
                "escalated", "planning", planning,
                session_id=planning.session_id, branch=event.branch,
            )

        if addon_prefix is None:
            addon_prefix = addon_prefix_from_spec(
                self.workspace.read(event.spec_path or ""), planning.feature
            )

        coder = self.coder or Coder(self.driver, self.workspace, gate1=self.gate1)
        session_id = planning.session_id
        if session_id is None:
            session_id = self.driver.client.create_session(
                title=f"impl/{planning.feature}"
            ).id
        # Persist the session so a later reporter comment re-enters it (Phase D).
        save_session(
            self.workspace,
            planning.feature,
            SessionRecord(session_id, addon_prefix),
        )
        self._provision(session_id, event.branch or "")
        impl = coder.implement(session_id, addon_prefix)
        return FlowResult(
            impl.status, "implement", planning, impl,
            session_id=session_id, branch=event.branch,
        )

    def reporter_iteration(self, event: Event) -> IterationResult:
        """Handle a reporter's PR comment (Phase D).

        Classify the comment, then act: a change request re-enters the saved
        OpenCode session and re-runs `/implement` with the delta; a question
        escalates to a human; an approval is acknowledged; noise is ignored.
        """
        self.workspace.checkout(event.branch or "")
        feature = feature_name(event.branch)
        comment = event.comment or ""
        intent = self.classifier.classify(comment)

        if intent is CommentIntent.CHANGE_REQUEST:
            record = load_session(self.workspace, feature)
            if record is None:
                reason = "reporter-iteration-no-session"
                self.workspace.escalate(
                    reason,
                    "a change was requested but no OpenCode session is on "
                    "record for this PR",
                )
                return IterationResult(
                    intent,
                    "escalated",
                    comment=escalation_notice(
                        reason,
                        "I could not find the original implementation session "
                        "for this PR.",
                    ),
                )
            # Re-check out the branch, then feed the reporter's feedback into the
            # session and re-run the full coder loop so the iteration is
            # Odoo-validated like the initial implementation — not a bare /implement.
            self._provision(record.session_id, event.branch or "")
            self.driver.client.send_message(
                record.session_id,
                f"Reporter feedback on the PR — please address it:\n\n{comment}",
            )
            coder = self.coder or Coder(self.driver, self.workspace, gate1=self.gate1)
            impl = coder.implement(record.session_id, record.addon_prefix)
            if impl.status == "implemented":
                return IterationResult(
                    intent,
                    "iterated",
                    comment=iteration_update(
                        "Re-implemented with your requested change."
                    ),
                    session_id=record.session_id,
                    branch=event.branch,
                )
            return IterationResult(
                intent,
                "escalated",
                comment=escalation_notice(
                    "reporter-iteration-failed-validation",
                    "I re-implemented your change, but it still fails Odoo "
                    "validation — flagging for a human.",
                ),
                session_id=record.session_id,
                branch=event.branch,
            )

        if intent is CommentIntent.QUESTION:
            reason = "reporter-question"
            self.workspace.escalate(
                reason, "a reporter asked a question that needs a human response"
            )
            return IterationResult(
                intent,
                "escalated",
                comment=escalation_notice(
                    reason,
                    "This looks like a question — I've flagged it for a human "
                    "teammate.",
                ),
            )

        if intent is CommentIntent.APPROVAL:
            return IterationResult(intent, "acknowledged")

        return IterationResult(intent, "ignored")
