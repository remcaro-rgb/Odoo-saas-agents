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
from .cost import Budget
from .drafter import DraftedSpec, Drafter
from .dup_detector import (
    DuplicateDetector,
    DuplicateResult,
    KnowledgeBase,
    render_duplicate_callout,
    title_prefix_for,
)
from .events import Event, EventType
from .intake import Intake, IntakeBuilder
from .prompt_injection import scan as scan_injection
from .refiner import IterationOutcome, Refiner
from .repro import AgentlabClient, Reproducer, ReproOutcome
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
    PROMPT_INJECTION = "prompt_injection"  # Tier 6 — adversarial content
    SPEND_CAP_REACHED = "spend_cap_reached"  # Tier 6 — $50/week hard stop


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
        refiner: Refiner | None = None,
        agentlab: AgentlabClient | None = None,
        budget: Budget | None = None,
        knowledge_base: KnowledgeBase | None = None,
        shadow: bool = True,
    ) -> None:
        self.oc = oc_client
        self.driver = SpecKitFrontDriver(oc_client)
        self.drafter = drafter or Drafter(self.driver)
        self.classifier = classifier or HeuristicClassifier()
        self.intake_builder = intake_builder or IntakeBuilder()
        self.refiner = refiner or Refiner(driver=self.driver)
        self.reproducer = Reproducer(agentlab) if agentlab is not None else None
        self.budget = budget
        # Tier 5 dup detector — `None` when the KB / embedding client isn't
        # provisioned in this deployment. The orchestrator skips dup-check
        # in that case (the pre-Tier-5 safe posture).
        self.dup_detector = (
            DuplicateDetector(kb=knowledge_base) if knowledge_base is not None else None
        )
        self.shadow = shadow

    def refine(
        self,
        *,
        comment: str,
        session_id: str,
        model: str | None = None,
    ) -> IterationOutcome:
        """Apply a reporter comment to an existing spec (Tier 2 refiner).

        Thin wrapper around `Refiner.apply` — the orchestrator owns the
        OpenCode session id so the caller doesn't need to know how the
        driver was constructed.
        """
        return self.refiner.apply(
            comment=comment, session_id=session_id, model=model
        )

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

        # Tier 6: prompt-injection scan runs BEFORE the LLM ever sees the body
        # so a successful detection means the orchestrator refuses to draft
        # — no model exposure, no inadvertent label-application. Detection
        # is logged via the result's notes for the security audit queue.
        injection = scan_injection(f"{intake.title}\n{intake.body}")
        if injection.triggered:
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.PROMPT_INJECTION,
                comments=[(
                    "issue",
                    "I've flagged this issue for a security review — it "
                    "contains content that looks like an attempt to "
                    "override the agent's instructions. A human teammate "
                    "will pick it up.",
                )],
                labels=[("issue", "needs-security-triage")],
                notes=[
                    f"prompt-injection categories: {', '.join(injection.categories)}",
                ],
            )

        # Tier 6: spend cap. If we've blown the weekly budget, refuse new
        # drafts until rollover. The exception path is sensitive-content +
        # injection — those are pure-logic, no LLM call, so they always run.
        if self.budget is not None:
            decision = self.budget.decide()
            if not decision.admit:
                return DraftResult(
                    status="escalated",
                    issue=intake.issue,
                    skip_reason=SkipReason.SPEND_CAP_REACHED,
                    comments=[(
                        "issue",
                        "I've hit my weekly spend cap. A human teammate "
                        "will pick this up; I'll resume drafts on the "
                        "next budget rollover.",
                    )],
                    labels=[("issue", "needs-human")],
                    notes=[decision.reason],
                )

        kind = self.classifier.classify(intake)

        if kind.kind is IntakeKind.SENSITIVE:
            return self._escalate_sensitive(intake, kind)

        if kind.kind is IntakeKind.FEATURE:
            return self._draft_feature(intake, kind, model=model)

        if kind.kind is IntakeKind.BUG:
            return self._handle_bug(intake, kind)

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

    def _handle_bug(self, intake: Intake, kind: KindResult) -> DraftResult:
        """Tier 4 bug flow: pre-flight, repro on agentlab, optionally draft a fix-brief."""
        if self.reproducer is None:
            # Tier 4 not yet provisioned in this deployment — fall back to
            # human triage (the pre-Tier-4 posture).
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.UNSUPPORTED_KIND,
                kind=kind,
                comments=[(
                    "issue",
                    "I see this looks like a bug report. I can't run bug "
                    "reproductions in this environment yet — a human "
                    "teammate will pick it up.",
                )],
                labels=[("issue", "needs-human")],
                notes=["bug intake -> human triage (no agentlab client wired)"],
            )

        repro = self.reproducer.attempt(intake)

        if repro.outcome is ReproOutcome.NEEDS_FIXTURE:
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.UNSUPPORTED_KIND,
                kind=kind,
                comments=[(
                    "issue",
                    "I can't reproduce this on my own because the steps "
                    "look like they need access to customer data. I've "
                    "routed this to the security-leads team — they'll "
                    "produce a sanitised fixture and pick it back up.",
                )],
                labels=[("issue", "needs-security-triage")],
                notes=[repro.summary or "needs sanitised fixture"],
            )

        if repro.outcome is ReproOutcome.NEEDS_REPRO_INFO:
            body_lines = [
                "Thanks for the report. Before I can reproduce this, I "
                "need a bit more information:",
                "",
            ]
            body_lines += [f"- {q}" for q in repro.questions]
            body_lines += [
                "",
                "Once you reply with the missing details I'll attempt the "
                "reproduction and post a fix-brief.",
            ]
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.UNSUPPORTED_KIND,
                kind=kind,
                comments=[("issue", "\n".join(body_lines))],
                labels=[("issue", "needs-repro-info")],
                notes=[repro.summary or "incomplete reproduction details"],
            )

        if repro.outcome is ReproOutcome.AGENTLAB_UNAVAILABLE:
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.EMPTY_DRAFT,
                kind=kind,
                comments=[(
                    "issue",
                    "I tried to reproduce this on agentlab but the runner "
                    "was unreachable. A human teammate will pick it up.",
                )],
                labels=[("issue", "needs-human")],
                notes=[repro.summary or "agentlab shim unreachable"],
            )

        # REPRO_CONFIRMED -> draft a fix-brief.
        try:
            drafted = self.drafter.draft_fix_brief(intake=intake, repro=repro)
        except Exception as exc:  # noqa: BLE001
            return DraftResult(
                status="escalated",
                issue=intake.issue,
                skip_reason=SkipReason.EMPTY_DRAFT,
                kind=kind,
                comments=[(
                    "issue",
                    "I reproduced the bug but couldn't render the fix-brief. "
                    "A human teammate will pick it up.",
                )],
                labels=[("issue", "needs-human")],
                notes=[f"drafter.draft_fix_brief raised {type(exc).__name__}: {exc}"],
            )

        return DraftResult(
            status="drafted",
            issue=intake.issue,
            kind=kind,
            drafted=drafted,
            comments=[(
                "issue",
                spec_drafted(
                    spec_path=drafted.path,
                    pr_number=None,
                    captured_items=list(drafted.captured_items),
                    open_questions=[],
                ),
            )],
            labels=[
                ("issue", SPEC_DRAFTED_LABEL),
                ("issue", AWAITING_CONFIRM_LABEL),
            ],
            notes=[
                f"fix-brief drafted on branch {drafted.branch} "
                f"({len(drafted.body)} chars, "
                f"{len(repro.screenshots)} screenshot(s))",
            ],
        )

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
        # Tier 5 dup detection — runs BEFORE the OpenCode draft so a clear
        # duplicate at ≥ 0.85 cosine still gets a spec PR (the reporter may
        # disagree with the detector) but the PR title is prefixed
        # ``[possible-dup]`` and a callout linking the original is appended
        # to the spec body. Skipped silently when no KB is wired.
        dup_result: DuplicateResult | None = None
        if self.dup_detector is not None:
            try:
                dup_result = self.dup_detector.detect(intake)
            except Exception as exc:  # noqa: BLE001
                # Detector failures must never block drafting — they
                # downgrade silently. The note surfaces in the audit log.
                dup_result = None
                _dup_failure_note = f"dup detector raised {type(exc).__name__}: {exc}"
            else:
                _dup_failure_note = ""
        else:
            _dup_failure_note = ""

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

        # Tier 5 dup result -> body callout + title prefix on the drafted spec.
        if dup_result is not None and dup_result.candidates:
            callout = render_duplicate_callout(dup_result)
            if callout:
                # Prepend the callout to the spec body so reviewers see it
                # at the top of the PR diff. Two newlines preserve markdown.
                drafted.body = callout + "\n\n" + drafted.body
            prefix = title_prefix_for(dup_result)
            if prefix:
                drafted.title_prefix = prefix

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
