"""Classify a reporter's PR comment (Phase D).

The reporter-iteration loop routes on a comment's intent. The default classifier
is deterministic — model-agnostic and unit-testable. An OpenCode-subagent
classifier can be swapped in behind the `Classifier` Protocol later (plan:
"classifier can be an OpenCode subagent").
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, runtime_checkable


class CommentIntent(StrEnum):
    """What a reporter's PR comment is asking the agent to do."""

    CHANGE_REQUEST = "change_request"  # the implementation should change
    APPROVAL = "approval"              # the reporter is satisfied
    QUESTION = "question"              # the reporter is asking something
    NOISE = "noise"                    # nothing actionable


@runtime_checkable
class Classifier(Protocol):
    """Maps a reporter comment to a `CommentIntent`."""

    def classify(self, comment: str) -> CommentIntent: ...


# Change-request signals — reasonably unambiguous "please change it" verbs.
# `change`/`move`/`fix` are phrase-anchored (`change the`, `move it`, ...) so the
# bare words don't fire on approvals like "nothing to change here" / "move on".
_CHANGE = re.compile(
    r"\b(?:instead|rename|remove|delete|replace|revert|swap|"
    r"change\s+(?:it|the|this|that)|move\s+(?:it|the|this|that)|"
    r"fix\s+(?:it|the|this|that)|make\s+it|needs?\s+to|"
    r"don'?t|do\s+not|doesn'?t|does\s+not|should\s+be|shouldn'?t|wrong)\b",
    re.IGNORECASE,
)
# Approval signals.
_APPROVAL = re.compile(
    r"\b(?:lgtm|looks?\s+good|ship\s+it|approved?|"
    r"perfect|ready\s+to\s+merge|merge\s+it)\b",
    re.IGNORECASE,
)
# A leading interrogative word.
_QUESTION_LEAD = re.compile(
    r"^\s*(?:why|how|what|when|where|who|which|"
    r"can|could|should|would|is|are|does|do|did)\b",
    re.IGNORECASE,
)


class HeuristicClassifier:
    """Deterministic keyword classifier — the default, model-agnostic impl.

    Precedence: change-request > question > approval > noise. A change request
    anywhere in the comment wins — acting on it (or failing to) is the costliest
    thing to get wrong. Swap in an OpenCode-subagent `Classifier` for finer
    judgement on ambiguous comments.
    """

    def classify(self, comment: str) -> CommentIntent:
        text = (comment or "").strip()
        if not text:
            return CommentIntent.NOISE
        if _CHANGE.search(text):
            return CommentIntent.CHANGE_REQUEST
        if text.endswith("?") or _QUESTION_LEAD.search(text):
            return CommentIntent.QUESTION
        if _APPROVAL.search(text):
            return CommentIntent.APPROVAL
        return CommentIntent.NOISE
