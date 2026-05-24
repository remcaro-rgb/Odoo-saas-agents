"""Spend tracking + $50/week cap (Tier 6).

The OpenCode Go quota is a soft signal — it tells us the orchestrator's per-
call cost but doesn't enforce a budget across runs. The Spec Generator's
hard stop is a $50/week ceiling configured per the design §8: when the
rolling 7-day spend crosses the threshold, the orchestrator refuses new
draft requests and pages on-call via Slack.

The cost ledger is intentionally Protocol-based — the production wire is
the `spec_generator_runs.cost_usd` column updated by the orchestrator after
every OpenCode call; tests use an in-memory ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

DEFAULT_WEEKLY_CAP_USD = 50.0
WARN_FRACTION = 0.8  # Slack page at 80% of cap (design §8).


@dataclass(frozen=True)
class SpendDecision:
    """Outcome of asking the budget gate to admit a new draft."""

    admit: bool
    spent_week_usd: float
    cap_usd: float
    warning: bool  # True if >= WARN_FRACTION * cap
    reason: str

    @property
    def fraction(self) -> float:
        return self.spent_week_usd / self.cap_usd if self.cap_usd else 0.0


@runtime_checkable
class CostLedger(Protocol):
    """The ledger surface the budget gate uses."""

    def spent_since(self, since: datetime) -> float: ...
    def record(self, *, amount_usd: float, when: datetime | None = None) -> None: ...


class InMemoryCostLedger:
    """In-memory ledger for tests / fixtures."""

    def __init__(self) -> None:
        self._records: list[tuple[datetime, float]] = []

    def record(self, *, amount_usd: float, when: datetime | None = None) -> None:
        self._records.append((when or datetime.now(UTC), float(amount_usd)))

    def spent_since(self, since: datetime) -> float:
        return sum(amount for ts, amount in self._records if ts >= since)


@dataclass
class Budget:
    """The $50/week gate."""

    ledger: CostLedger
    weekly_cap_usd: float = DEFAULT_WEEKLY_CAP_USD

    def decide(self, *, now: datetime | None = None) -> SpendDecision:
        """Should the orchestrator admit a new draft request?"""
        now = now or datetime.now(UTC)
        since = now - timedelta(days=7)
        spent = self.ledger.spent_since(since)
        admit = spent < self.weekly_cap_usd
        warning = spent >= WARN_FRACTION * self.weekly_cap_usd
        if not admit:
            reason = (
                f"weekly spend ${spent:.2f} >= cap ${self.weekly_cap_usd:.2f} "
                f"— refuse-to-draft until rollover"
            )
        elif warning:
            reason = (
                f"weekly spend ${spent:.2f} >= 80% of cap "
                f"${self.weekly_cap_usd:.2f} — page on-call"
            )
        else:
            reason = (
                f"weekly spend ${spent:.2f} of cap ${self.weekly_cap_usd:.2f}"
            )
        return SpendDecision(
            admit=admit, spent_week_usd=spent,
            cap_usd=self.weekly_cap_usd, warning=warning,
            reason=reason,
        )

    def record(self, *, amount_usd: float, when: datetime | None = None) -> None:
        self.ledger.record(amount_usd=amount_usd, when=when)
