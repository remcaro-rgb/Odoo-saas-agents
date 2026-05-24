"""Reporter Q&A loop — Tier 2 (the back half of the iterate flow).

The Spec Generator's spec PR is the iteration locus. When a reporter posts a
comment on the issue or on the spec PR, the agent maps the comment to an
intent and acts:

  - ``/confirm`` (or a clearly-approving comment) → apply ``intent-confirmed``;
    the Implementation Agent picks it up via its own webhook.
  - ``/reclassify bug`` / ``/reclassify feature`` → reset the routing kind,
    re-run the drafter against the original intake. Tier 4 honors the
    ``bug`` variant by running ``draft_fix_brief``; until then we comment
    that the reclassify will route to human triage for bug.
  - A clarifying answer → re-enter the OpenCode session, run
    ``/speckit.clarify`` with the reply, post the updated capture/open-questions
    summary back to the PR. The same session id is preserved across the
    full life of the PR so context stays warm.
  - Noise (a thank-you, a question to a human, etc.) → no-op.

The refiner is pure-logic: it does not call GitHub, it returns an
``IterationOutcome`` that the github_io layer maps to comments/labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .commenter import awaiting_reporter_confirm
from .speckit_driver import SpecKitFrontDriver

# `intent-confirmed` is the label that hands the spec PR to the Implementation
# Agent (its webhook fires on `pull_request.labeled` with this exact name).
INTENT_CONFIRMED_LABEL = "intent-confirmed"
# Removed once the agent has acted on a clarifying comment.
AWAITING_CONFIRM_LABEL = "awaiting-reporter-confirm"
AWAITING_RECONFIRM_LABEL = "awaiting-reporter-reconfirm"


class CommentIntent(StrEnum):
    """What the reporter is asking the agent to do with their comment."""

    CONFIRM = "confirm"               # `/confirm` — ship the spec as-is
    RECLASSIFY_BUG = "reclassify_bug"  # `/reclassify bug`
    RECLASSIFY_FEATURE = "reclassify_feature"  # `/reclassify feature`
    CLARIFY = "clarify"               # a clarifying answer (free text)
    APPROVAL = "approval"             # "lgtm" / "looks good" — alias for confirm
    NOISE = "noise"                   # thank-you, off-topic, blank


@dataclass
class IterationOutcome:
    """What the refiner thinks the github_io layer should do.

    `status` is one of:
      - `"handed_off"` — `/confirm` or approval; apply `intent-confirmed`.
      - `"reclassified"` — `/reclassify X`; reset and re-route.
      - `"clarified"` — re-ran `/speckit.clarify`; the updated spec text
        + new captured/open-questions live on `drafted`.
      - `"ignored"` — noise; no work.
    """

    intent: CommentIntent
    status: str
    comments: list[tuple[str, str]] = field(default_factory=list)
    labels: list[tuple[str, str]] = field(default_factory=list)
    labels_to_remove: list[tuple[str, str]] = field(default_factory=list)
    new_kind: str | None = None       # for reclassify outcomes
    spec_text: str = ""               # for clarified outcomes
    open_questions: tuple[str, ...] = ()
    captured_items: tuple[str, ...] = ()
    session_id: str | None = None
    notes: list[str] = field(default_factory=list)


# Approval signals — borrowed from the Implementation Agent's classifier.
_APPROVAL = re.compile(
    r"\b(?:lgtm|looks?\s+good|ship\s+it|approved?|"
    r"perfect|ready\s+to\s+merge|merge\s+it|"
    r"that\s+works|sounds?\s+good)\b",
    re.IGNORECASE,
)

_CONFIRM = re.compile(r"(?:^|\s)/confirm\b", re.IGNORECASE)
_RECLASSIFY = re.compile(
    r"(?:^|\s)/reclassify\s+(bug|feature)\b", re.IGNORECASE
)


@runtime_checkable
class CommentClassifier(Protocol):
    """Maps a reporter comment to a `CommentIntent`."""

    def classify(self, comment: str) -> CommentIntent: ...


class HeuristicCommentClassifier:
    """Deterministic comment classifier — the Tier 2 default.

    Precedence (high to low):
      1. ``/confirm`` slash command.
      2. ``/reclassify bug`` / ``/reclassify feature`` slash command.
      3. Approval phrases ("lgtm", "looks good", "ship it") -> treat as confirm.
      4. Non-empty free text -> CLARIFY (a clarifying answer / new context).
      5. Empty / whitespace -> NOISE.
    """

    def classify(self, comment: str) -> CommentIntent:
        text = (comment or "").strip()
        if not text:
            return CommentIntent.NOISE
        if _CONFIRM.search(text):
            return CommentIntent.CONFIRM
        m = _RECLASSIFY.search(text)
        if m:
            return (
                CommentIntent.RECLASSIFY_BUG
                if m.group(1).lower() == "bug"
                else CommentIntent.RECLASSIFY_FEATURE
            )
        if _APPROVAL.search(text):
            return CommentIntent.APPROVAL
        return CommentIntent.CLARIFY


@dataclass
class Refiner:
    """Co-ordinates `/speckit.clarify` + intent handling for reporter comments.

    Stateful only in that it holds the OpenCode driver + the comment
    classifier. Session ownership stays with the caller (the orchestrator)
    so the same session is reused across the full PR life.
    """

    driver: SpecKitFrontDriver
    classifier: CommentClassifier = field(default_factory=HeuristicCommentClassifier)

    def apply(
        self,
        *,
        comment: str,
        session_id: str,
        model: str | None = None,
    ) -> IterationOutcome:
        """Classify the reporter's `comment` and produce an `IterationOutcome`."""
        intent = self.classifier.classify(comment)

        if intent in (CommentIntent.CONFIRM, CommentIntent.APPROVAL):
            return IterationOutcome(
                intent=intent,
                status="handed_off",
                labels=[("pr", INTENT_CONFIRMED_LABEL)],
                labels_to_remove=[("pr", AWAITING_CONFIRM_LABEL)],
                comments=[(
                    "pr",
                    "Thanks — handing this off to the Implementation Agent. "
                    "It will open another PR with the actual code change.",
                )],
                notes=["reporter confirmed; intent-confirmed label applied"],
            )

        if intent in (CommentIntent.RECLASSIFY_BUG, CommentIntent.RECLASSIFY_FEATURE):
            new_kind = "bug" if intent is CommentIntent.RECLASSIFY_BUG else "feature"
            return IterationOutcome(
                intent=intent,
                status="reclassified",
                new_kind=new_kind,
                comments=[(
                    "pr",
                    f"Got it — re-classifying this as **{new_kind}** and "
                    f"redrafting. I'll post the new spec shortly.",
                )],
                notes=[f"reporter forced reclassification to {new_kind}"],
            )

        if intent is CommentIntent.NOISE:
            return IterationOutcome(
                intent=intent,
                status="ignored",
                notes=["empty / noise comment — no action"],
            )

        # CLARIFY: re-enter the session, run /speckit.clarify, post update.
        try:
            result = self.driver.run_clarify(session_id, comment, model=model)
        except Exception as exc:  # noqa: BLE001
            return IterationOutcome(
                intent=intent,
                status="ignored",
                comments=[(
                    "pr",
                    "I hit an error trying to apply your feedback. A human "
                    "teammate will take a look — sorry for the noise.",
                )],
                labels=[("pr", "needs-human")],
                notes=[f"clarify raised {type(exc).__name__}: {exc}"],
            )

        if not result.spec_text.strip():
            return IterationOutcome(
                intent=intent,
                status="ignored",
                comments=[(
                    "pr",
                    "I tried to update the spec but OpenCode returned no "
                    "content. A human teammate will take a look.",
                )],
                labels=[("pr", "needs-human")],
                notes=["/speckit.clarify produced empty output"],
            )

        captured = _captured_items_from(result.spec_text)
        labels_to_add: list[tuple[str, str]] = []
        labels_to_remove: list[tuple[str, str]] = []
        if not result.open_questions:
            # All open questions resolved — restore the awaiting-confirm
            # posture so the sweep (Tier 3) can pick it up if the reporter
            # goes silent.
            labels_to_remove.append(("pr", AWAITING_RECONFIRM_LABEL))
            labels_to_add.append(("pr", AWAITING_CONFIRM_LABEL))
        else:
            # New / remaining questions — make the reporter aware they owe a
            # reply before the sweep can hand off.
            labels_to_remove.append(("pr", AWAITING_CONFIRM_LABEL))
            labels_to_add.append(("pr", AWAITING_RECONFIRM_LABEL))

        return IterationOutcome(
            intent=intent,
            status="clarified",
            spec_text=result.spec_text,
            open_questions=result.open_questions,
            captured_items=captured,
            session_id=session_id,
            comments=[(
                "pr",
                awaiting_reporter_confirm(
                    captured_items=list(captured),
                    open_questions=list(result.open_questions),
                ),
            )],
            labels=labels_to_add,
            labels_to_remove=labels_to_remove,
            notes=[
                f"clarified ({len(result.spec_text)} chars, "
                f"{len(result.open_questions)} open-questions)",
            ],
        )


_BULLET = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)


def _captured_items_from(text: str, *, limit: int = 6) -> tuple[str, ...]:
    """Pull the first `limit` non-marker bullet items from a refined spec.

    Mirrors the Tier-1 `SpecKitFrontDriver` extractor — kept local here so
    a future drift in the refined-spec shape (Tier 3 may add headings)
    doesn't force the Tier-1 reader to budge.
    """
    items: list[str] = []
    for match in _BULLET.finditer(text):
        item = match.group(1).strip()
        if not item or item.startswith("[NEEDS"):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return tuple(items)


# Backwards-compat: import surface for the package.
__all__ = [
    "AWAITING_CONFIRM_LABEL",
    "AWAITING_RECONFIRM_LABEL",
    "INTENT_CONFIRMED_LABEL",
    "CommentClassifier",
    "CommentIntent",
    "HeuristicCommentClassifier",
    "IterationOutcome",
    "Refiner",
]


# Expose a typed Any-ish forward decl for IDE comfort — not strictly needed.
_: Any = None
