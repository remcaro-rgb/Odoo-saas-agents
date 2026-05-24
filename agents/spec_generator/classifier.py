"""Classify an `Intake` into a routing kind (Tier 1).

The spec generator routes on five kinds (design spec §5.1.1):

- `feature` — a feature request -> `draft_design_spec` flow.
- `bug` — a defect report -> `repro_attempt` then `draft_fix_brief` (Tier 4).
- `config` — a tenant configuration question, no code change needed -> escalate
  to support inbox.
- `user_error` — the reporter is asking the agent to do something the system
  already supports -> escalate to support with a docs link.
- `sensitive` — the body contains secrets, customer data, or anything the
  agent should not autonomously summarise -> route to `security-leads`
  CODEOWNERS and do not draft.

The default `HeuristicClassifier` is deterministic: it trusts the label hint
when present, otherwise uses keyword + structural cues. A frontier-model
`LLMClassifier` can be swapped in via the `Classifier` Protocol later (Tier 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .intake import Intake


class IntakeKind(StrEnum):
    """How the orchestrator should handle an intake."""

    FEATURE = "feature"
    BUG = "bug"
    CONFIG = "config"
    USER_ERROR = "user_error"
    SENSITIVE = "sensitive"


@dataclass(frozen=True)
class KindResult:
    """The classifier's verdict.

    `confidence` is a 0..1 score; the orchestrator refuses to draft below
    `min_confidence` (Tier 2 introduces a tunable threshold per design Q6).
    `signals` is the human-readable reason — surfaced into the bot's comment
    so reporters can correct a mis-route.
    """

    kind: IntakeKind
    confidence: float
    signals: tuple[str, ...] = ()


@runtime_checkable
class Classifier(Protocol):
    """Maps an `Intake` to a `KindResult`."""

    def classify(self, intake: Intake) -> KindResult: ...


# Sensitive content — secrets, PII, customer data. These patterns force the
# classifier to refuse the draft regardless of label hints (defence in depth
# against a reporter accidentally pasting a `.env` excerpt into the body).
_SENSITIVE = re.compile(
    r"\b(?:password|passwd|secret|api[_-]?key|private[_-]?key|"
    r"BEGIN\s+(?:RSA|EC|OPENSSH|PRIVATE)\s+KEY|"
    r"AKIA[0-9A-Z]{16}|"  # AWS access key prefix
    r"sk_(?:live|test)_[0-9a-zA-Z]{24,}|"  # Stripe-style secrets
    r"ssn|social\s+security|"
    r"credit\s+card|cardholder)\b",
    re.IGNORECASE,
)

# Bug signals — language a reporter uses about a defect.
_BUG = re.compile(
    r"\b(?:bug|crash|crashes|crashed|broken|error|exception|"
    r"stack\s*trace|traceback|500|503|"
    r"not\s+working|doesn'?t\s+work|fails?\s+to|"
    r"reproduce|repro|regress(?:ion)?)\b",
    re.IGNORECASE,
)

# Feature signals — language about wanting something new.
_FEATURE = re.compile(
    r"\b(?:add|new\s+feature|feature\s+request|would\s+like|"
    r"please\s+(?:add|build|implement|support)|"
    r"can\s+(?:you|we)\s+(?:add|build|implement|support)|"
    r"missing|enhance(?:ment)?)\b",
    re.IGNORECASE,
)

# Config / how-do-I-do signals — a question, not a request for code.
_CONFIG = re.compile(
    r"\b(?:how\s+do\s+I|how\s+can\s+I|how\s+to|configure?|setting|"
    r"setup|set\s+up|enable|disable|where\s+is)\b",
    re.IGNORECASE,
)


def _label_hint(intake: Intake) -> IntakeKind | None:
    """Trust a routing label when present.

    A human applied the label, so the explicit signal beats any heuristic.
    `sensitive` is the only override: even if a human said `feature-request`,
    the sensitive-content scan still wins (defence in depth, design §8).
    """
    hint = (intake.kind_hint or "").strip().lower()
    if hint == "bug":
        return IntakeKind.BUG
    if hint == "feature-request":
        return IntakeKind.FEATURE
    for label in intake.labels:
        lower = label.lower()
        if lower == "bug":
            return IntakeKind.BUG
        if lower == "feature-request":
            return IntakeKind.FEATURE
    return None


class HeuristicClassifier:
    """Deterministic keyword classifier — the Tier 1 default.

    Precedence (high to low):
      1. Sensitive content scan (regardless of label).
      2. Routing label hint (`bug` / `feature-request`).
      3. Bug keywords in the body.
      4. Feature keywords.
      5. Config-question keywords.
      6. Fall back to `feature` with low confidence — the bot will surface
         the low confidence in its `summary_back` so the reporter can override.
    """

    def classify(self, intake: Intake) -> KindResult:
        text = f"{intake.title}\n{intake.body}".strip()

        # Sensitive content beats any label — refuse the draft, route to security.
        sensitive_hits = _SENSITIVE.findall(text)
        if sensitive_hits:
            return KindResult(
                kind=IntakeKind.SENSITIVE,
                confidence=0.95,
                signals=tuple(f"sensitive:{hit.lower()}" for hit in sensitive_hits[:3]),
            )

        # A human-applied routing label is the strongest non-sensitive signal.
        if (hinted := _label_hint(intake)) is not None:
            return KindResult(
                kind=hinted,
                confidence=0.9,
                signals=(f"label:{intake.kind_hint or 'live-labels'}",),
            )

        # Body heuristics.
        if _BUG.search(text):
            return KindResult(
                kind=IntakeKind.BUG,
                confidence=0.7,
                signals=("keywords:bug",),
            )
        if _FEATURE.search(text):
            return KindResult(
                kind=IntakeKind.FEATURE,
                confidence=0.7,
                signals=("keywords:feature",),
            )
        if _CONFIG.search(text):
            return KindResult(
                kind=IntakeKind.CONFIG,
                confidence=0.6,
                signals=("keywords:config",),
            )

        # Low-confidence fallback. The orchestrator will draft anyway in Tier
        # 1 — the comment surfaces the uncertainty. Tier 2 raises a threshold.
        return KindResult(
            kind=IntakeKind.FEATURE,
            confidence=0.3,
            signals=("fallback:no-signals",),
        )
