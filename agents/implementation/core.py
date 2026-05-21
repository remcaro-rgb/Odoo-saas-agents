"""The orchestrator core (Phase B).

Routes a normalized `Event` to a flow, and runs the **planning half** of the
implement flow: map the spec to Spec-Kit form, drive `/plan` -> `/tasks` ->
`/analyze`, escalate on incoherence, and commit the planning artifacts.

Fix-briefs take a fast-path that skips `/plan` + `/tasks` (plan decision: the
Spec-Kit pipeline is ~10x heavier than needed for a tiny fix).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .coder import Coder, ImplementResult
from .events import Event, EventType, SpecKind
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


def route(event: Event) -> str:
    """Map an event to its flow name. Only `implement` is built in Phase B."""
    return _FLOWS.get(event.type, "unsupported")


def feature_name(branch: str | None) -> str:
    """Derive a Spec-Kit feature name from a branch (`agent/spec-1500` -> `spec-1500`)."""
    return (branch or "").rsplit("/", 1)[-1] or "feature"


class Orchestrator:
    """Drives the planning half of the implement flow over a Workspace + driver."""

    def __init__(
        self,
        workspace: Workspace,
        driver: SpecKitDriver,
        coder: Coder | None = None,
    ) -> None:
        self.workspace = workspace
        self.driver = driver
        self.coder = coder

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

    def implement(self, event: Event, addon_prefix: str) -> FlowResult:
        """The full implement flow: planning, then hand off to the coder loop.

        Planning and coding share one OpenCode session — a design spec's session
        is reused; a fix-brief (which skipped planning) gets one created here.
        """
        planning = self.run_planning(event)
        if planning.status == "escalated":
            return FlowResult("escalated", "planning", planning)

        coder = self.coder or Coder(self.driver, self.workspace)
        session_id = planning.session_id
        if session_id is None:
            session_id = self.driver.client.create_session(
                title=f"impl/{planning.feature}"
            ).id
        impl = coder.implement(session_id, addon_prefix)
        return FlowResult(impl.status, "implement", planning, impl)
