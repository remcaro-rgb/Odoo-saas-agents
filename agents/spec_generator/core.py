"""Spec Generator orchestrator — event routing + flow sequencing.

`Orchestrator.draft_spec` is the Tier-1 happy path for a feature-request issue:

  Event -> Intake -> KindResult -> (sensitive? -> escalate)
                                |
                                +-> (feature? -> DraftedSpec via /speckit.specify)
                                |
                                +-> (bug? -> Tier 4 fix-brief, currently SKIP)
                                |
                                +-> (config/user_error? -> escalate to support)

The caller (`github_io.handle_webhook`) is responsible for the GitHub-side
writes — opening the PR, posting the comment, applying labels. The
orchestrator returns a `DraftResult` describing what *should* be done so the
SHADOW client can record the intent without firing it.

Bug repro (`draft_fix_brief`), the auto-confirm sweep, and the reporter Q&A
loop ship in later tiers — see ``docs/SPEC-GEN-TIER-1-RUNBOOK.md``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from agents.implementation.opencode_client import OpenCodeClient

from .classifier import Classifier, HeuristicClassifier, IntakeKind, KindResult
from .commenter import (
    low_confidence_notice,
    sensitive_escalation_notice,
    spec_drafted,
)
from .drafter import DraftedSpec, Drafter
from .events import Event, EventType
from .intake import Intake, IntakeBuilder
from .speckit_driver import SpecKitFrontDriver

# The label the auto-confirm sweep (Tier 3) waits on / advances. Documented
# here so Tier 1 places the right label on a drafted PR even though the
# sweep itself isn't shipped yet.
AWAITING_CONFIRM_LABEL = "awaiting-reporter-confirm"
SPEC_DRAFTED_LABEL = "spec-drafted"
SENSITIVE_ESCALATION_LABEL = "needs-security-triage"
LOW_CONFIDENCE_NOTICE_THRESHOLD = 0.65


class SkipReason(StrEnum):
    """Why the orchestrator declined to draft on this event."""

    NOT_A_TRIGGER = "not_a_trigger"  # webhook the orchestrator does not act on
    SENSITIVE_CONTENT = "sensitive_content"
    UNSUPPORTED_KIND = "unsupported_kind"  # config/user_error/bug-pre-Tier4
    EMPTY_DRAFT = "empty_draft"  # OpenCode returned nothing


def feature_name(branch: str) -> str:
    """Extract the slug part of an `agent/spec-NNNN-<slug>` branch.

    Mirrors `agents.implementation.core.feature_name` so the two agents read
    the same branch convention.
    """
    if not branch.startswith("agent/spec-"):
        return branch
    tail = branch[len("agent/spec-") :]
    parts = tail.split("-", 1)
    return parts[1] if len(parts) > 1 else tail


@dataclass
class DraftResult:
    """What the orchestrator would have the GitHub-side write back to GitHub.

    `status` is one of:
      - `"drafted"` — a spec was produced; open the PR + post the comment.
      - `"escalated"` — refuse-to-draft (sensitive / unsupported / empty).
      - `"skipped"` — webhook doesn't match (no work done).

    `comments` is a list of `(target, body)` tuples — target is `"issue"` to
    write on the originating issue, `"pr"` to write on the spec PR. `labels`
    is the same shape. The list-of-tuples form keeps the SHADOW client's
    record/replay extremely simple.
    """

    status: str
    issue: int | None
    skip_reason: SkipReason | None = None
    drafted: DraftedSpec | None = None
    kind: KindResult | None = None
    comments: list[tuple[str, str]] = field(default_factory=list)
    labels: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Orchestrator:
    """Wires intake -> classifier -> drafter -> result.

    `shadow` is informational only — the orchestrator does the same work in
    SHADOW and ACT; what differs is what the GitHub-side client (passed in by
    `github_io.handle_webhook`) does with the recorded intents. The flag is
    surfaced into `notes` for observability.
    """

    def __init__(
        self,
        *,
        oc_client: OpenCodeClient,
        drafter: Drafter | None = None,
        classifier: Classifier | None = None,
        intake_builder: IntakeBuilder | None = None,
        shadow: bool = True,
    ) -> None:
        self.oc = oc_client
        self.driver = SpecKitFrontDriver(oc_client)
        self.drafter = drafter or Drafter(self.driver)
        self.classifier = classifier or HeuristicClassifier()
        self.intake_builder = intake_builder or IntakeBuilder()
        self.shadow = shadow

    def draft_spec(
        self,
        event: Event,
        *,
        live_labels: Iterable[str] | None = None,
        model: str | None = None,
    ) -> DraftResult:
        """Run the Tier-1 happy path on an `issues.opened` / `issues.labeled` event.

        `live_labels` is the current label set on the issue (read by the
        caller from GitHub). The classifier uses it to break ties.
        """
        if event.type not in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
            return DraftResult(
                status="skipped",
                issue=event.issue,
                skip_reason=SkipReason.NOT_A_TRIGGER,
                notes=[f"event type {event.type} is not handled by Tier 1"],
            )
        if event.issue is None:
            return DraftResult(
                status="skipped",
                issue=None,
                skip_reason=SkipReason.NOT_A_TRIGGER,
                notes=["event carries no issue number"],
            )

        intake = self.intake_builder.build(event, labels=live_labels)
        kind = self.classifier.classify(intake)

        if kind.kind is IntakeKind.SENSITIVE:
            return self._escalate_sensitive(intake, kind)

        if kind.kind is IntakeKind.FEATURE:
            return self._draft_feature(intake, kind, model=model)

        if kind.kind is IntakeKind.BUG:
            # Bug repro ships in Tier 4 — for Tier 1 we record the routing
            # without drafting. The Implementation Agent must not be invoked
            # for an unconfirmed bug.
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.UNSUPPORTED_KIND,
                kind=kind,
                comments=[(
                    "issue",
                    "I see this looks like a bug report. I can't draft fix-briefs "
                    "automatically yet (that ships in Tier 4 of the Spec "
                    "Generator). A human teammate will pick this up.",
                )],
                labels=[("issue", "needs-human")],
                notes=["bug intake -> human triage (Tier 4 deferred)"],
            )

        # config / user_error — route to support inbox, do not draft.
        return DraftResult(
            status="escalated",
            issue=intake.issue,
            skip_reason=SkipReason.UNSUPPORTED_KIND,
            kind=kind,
            comments=[(
                "issue",
                "This looks like a configuration or usage question. I'm "
                "routing it to the support inbox — they'll get back to you "
                "with the right answer or a link to docs.",
            )],
            labels=[("issue", "needs-support")],
            notes=[f"{kind.kind.value} intake -> support inbox"],
        )

    # -- internals --------------------------------------------------------

    def _escalate_sensitive(
        self, intake: Intake, kind: KindResult
    ) -> DraftResult:
        return DraftResult(
            status="escalated",
            issue=intake.issue,
            skip_reason=SkipReason.SENSITIVE_CONTENT,
            kind=kind,
            comments=[("issue", sensitive_escalation_notice(list(kind.signals)))],
            labels=[("issue", SENSITIVE_ESCALATION_LABEL)],
            notes=[
                f"sensitive content detected ({len(kind.signals)} signal(s)) — "
                "no draft, security-leads notified",
            ],
        )

    def _draft_feature(
        self, intake: Intake, kind: KindResult, *, model: str | None
    ) -> DraftResult:
        session = self.oc.create_session(title=f"spec-gen/{intake.issue}")
        try:
            drafted = self.drafter.draft_design_spec(
                intake=intake, session_id=session.id, model=model
            )
        except Exception as exc:  # noqa: BLE001
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.EMPTY_DRAFT,
                kind=kind,
                comments=[(
                    "issue",
                    "I hit an error trying to draft this spec. A human "
                    "teammate will pick this up — sorry for the noise.",
                )],
                labels=[("issue", "needs-human")],
                notes=[f"drafter raised {type(exc).__name__}: {exc}"],
            )

        if not drafted.body.strip():
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.EMPTY_DRAFT,
                kind=kind,
                drafted=drafted,
                comments=[(
                    "issue",
                    "I tried to draft this spec but OpenCode returned no "
                    "content. A human teammate will pick this up.",
                )],
                labels=[("issue", "needs-human")],
                notes=["/speckit.specify produced empty output"],
            )

        comments: list[tuple[str, str]] = [(
            "issue",
            spec_drafted(
                spec_path=drafted.path,
                pr_number=None,  # Tier 1 SHADOW: PR not actually opened
                captured_items=list(drafted.captured_items),
                open_questions=list(drafted.open_questions),
            ),
        )]
        if kind.confidence < LOW_CONFIDENCE_NOTICE_THRESHOLD:
            comments.append((
                "issue",
                low_confidence_notice(
                    kind.kind.value, kind.confidence, list(kind.signals)
                ),
            ))

        return DraftResult(
            status="drafted",
            issue=intake.issue,
            kind=kind,
            drafted=drafted,
            comments=comments,
            labels=[
                ("issue", SPEC_DRAFTED_LABEL),
                ("issue", AWAITING_CONFIRM_LABEL),
            ],
            notes=[
                f"drafted on branch {drafted.branch} "
                f"({len(drafted.body)} chars, "
                f"{len(drafted.open_questions)} open-questions)",
            ],
        )
