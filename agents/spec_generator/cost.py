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


class PostgresCostLedger:
    """`CostLedger` backed by ``spec_generator_runs.cost_usd`` on the
    control-plane Postgres.

    `spent_since` sums every row whose ``updated_at`` falls inside the
    rolling 7-day window. `record(amount_usd, when)` is a fire-and-forget
    UPDATE — the Tier 3 `PostgresRunStore.record_cost` is the canonical
    write path for actually adding cost to a row, this method writes to
    a single synthetic "sentinel" issue used purely for testing /
    backstop tracking when we don't have a per-issue ledger.

    Like other Postgres-backed stores in this package, errors are
    swallowed + logged: a control-plane outage degrades the spend gate
    to "no cap" (admit by default) rather than refusing every draft.
    """

    #: Synthetic issue_number used by the standalone `record()` write.
    #: Real per-issue costs are recorded via `PostgresRunStore.record_cost`
    #: which keyes on the actual GitHub issue number.
    SENTINEL_ISSUE = -1

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self):  # noqa: ANN202  (psycopg.Connection requires the runtime import)
        import psycopg

        return psycopg.connect(self.dsn, autocommit=True)

    def spent_since(self, since: datetime) -> float:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0)::float "
                    "FROM spec_generator_runs "
                    "WHERE updated_at >= %s",
                    (since,),
                )
                row = cur.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"warning: PostgresCostLedger.spent_since failed: {exc}",
                file=sys.stderr, flush=True,
            )
            return 0.0

    def record(self, *, amount_usd: float, when: datetime | None = None) -> None:
        """Record cost against the SENTINEL issue.

        In production the orchestrator uses
        ``PostgresRunStore.record_cost(issue, amount)`` to attribute spend
        to the originating GitHub issue. This method is a backstop for
        standalone tests and for tracking spend that has no natural
        issue parent (e.g. the ingest cron's OpenAI embedding calls).
        """
        sql = (
            "INSERT INTO spec_generator_runs "
            "  (issue_number, kind, confidence, branch, spec_path, "
            "   cost_usd, phase) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (issue_number) DO UPDATE SET "
            "  cost_usd = spec_generator_runs.cost_usd + EXCLUDED.cost_usd"
        )
        params = (
            self.SENTINEL_ISSUE, "sentinel", 0.0,
            "agent/sentinel", "docs/sentinel.md",
            float(amount_usd), "completed",
        )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"warning: PostgresCostLedger.record failed: {exc}",
                file=sys.stderr, flush=True,
            )


def build_budget(env: dict[str, str]) -> Budget | None:
    """Composition-root helper. Picks the cost ledger by env:

    1. ``CONTROL_PLANE_PG_DSN`` set + psycopg importable -> Postgres ledger.
    2. Otherwise ``None`` — the orchestrator skips the spend cap.

    ``SPEC_GEN_WEEKLY_CAP_USD`` overrides the default ``$50``.
    """
    dsn = (env.get("CONTROL_PLANE_PG_DSN") or "").strip()
    if not dsn:
        return None
    try:
        import psycopg  # noqa: F401
    except ImportError:
        import sys
        print(
            "warning: CONTROL_PLANE_PG_DSN set but psycopg unavailable; "
            "spend cap disabled",
            file=sys.stderr, flush=True,
        )
        return None
    cap = float(env.get("SPEC_GEN_WEEKLY_CAP_USD") or DEFAULT_WEEKLY_CAP_USD)
    return Budget(ledger=PostgresCostLedger(dsn), weekly_cap_usd=cap)


__all__ = [
    "Budget",
    "CostLedger",
    "DEFAULT_WEEKLY_CAP_USD",
    "InMemoryCostLedger",
    "PostgresCostLedger",
    "SpendDecision",
    "WARN_FRACTION",
    "build_budget",
]
