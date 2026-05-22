"""Per-PR cost tracking + the spend cap (Phase E, design §10.5 / §11).

The alternative plan drops the bespoke `LLMProvider.cost_per_1k_*` plumbing:
OpenCode reports a per-message `cost` in each session, so `session_cost` rolls
that up and `Budget` enforces the design's $20 per-PR cap — `warn` at 80%,
`exceeded` at 100% (the orchestrator then escalates with `budget-exceeded`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The design's per-PR sub-budget (planner + coder + iterations), in USD.
DEFAULT_CAP_USD = 20.0
#: Spend fraction at which the budget raises a warning (pings on-call).
WARN_FRACTION = 0.8


@dataclass
class Budget:
    """Accumulates a PR's LLM + compute spend against a hard cap.

    `add` rolls in each charge (a session's cost, a preview redeploy, ...).
    `warn` is True from 80% of the cap; `exceeded` is True at 100% — past which
    no further iterations run and the PR is escalated as `budget-exceeded`.
    """

    cap_usd: float = DEFAULT_CAP_USD
    spent_usd: float = 0.0

    def add(self, cost_usd: float) -> None:
        """Record a charge. A negative charge is ignored (never refunds the cap)."""
        self.spent_usd += max(cost_usd, 0.0)

    @property
    def remaining_usd(self) -> float:
        return max(self.cap_usd - self.spent_usd, 0.0)

    @property
    def fraction_used(self) -> float:
        return self.spent_usd / self.cap_usd if self.cap_usd > 0 else 1.0

    @property
    def warn(self) -> bool:
        """True once spend reaches the 80% warning threshold."""
        return self.fraction_used >= WARN_FRACTION

    @property
    def exceeded(self) -> bool:
        """True once the cap is reached — no further iterations are allowed."""
        return self.spent_usd >= self.cap_usd


def session_cost(messages: list[Any]) -> float:
    """Sum the per-message `cost` across an OpenCode session's messages.

    `messages` is the list from `OpenCodeClient.list_messages`. OpenCode records
    a USD `cost` (a float) on each assistant message's `info` — verified against
    OpenCode v1.15.7's `GET /session/:id/message` response. Messages without a
    numeric cost (e.g. user messages) contribute nothing.
    """
    total = 0.0
    for message in messages:
        if not isinstance(message, dict):
            continue
        cost = (message.get("info") or {}).get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total
