"""Unit tests for PostgresCostLedger + build_budget (Tier 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.spec_generator.cost import (
    DEFAULT_WEEKLY_CAP_USD,
    Budget,
    PostgresCostLedger,
    build_budget,
)


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, Any]],
                 fetch: tuple[Any, ...] | None = None):
        self._calls = calls
        self._fetch = fetch

    def execute(self, sql: str, params: Any = ()) -> None:
        self._calls.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._fetch

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, calls: list[tuple[str, Any]],
                 fetch: tuple[Any, ...] | None = None):
        self._calls = calls
        self._fetch = fetch

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._calls, self._fetch)

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def ledger(monkeypatch):
    pytest.importorskip("psycopg")
    calls: list[tuple[str, Any]] = []
    state: dict[str, Any] = {"fetch": None}
    store = PostgresCostLedger("postgresql://x@/y")
    monkeypatch.setattr(
        store, "_connect", lambda: _FakeConn(calls, state["fetch"])
    )
    return store, calls, state


# ---------------------------------------------------------------------------
# spent_since
# ---------------------------------------------------------------------------

def test_spent_since_executes_sum_over_window(ledger):
    store, calls, state = ledger
    state["fetch"] = (42.5,)
    since = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    spent = store.spent_since(since)
    assert spent == 42.5
    sql, params = calls[0]
    assert "SUM(cost_usd)" in sql
    assert "updated_at >= %s" in sql
    assert params == (since,)


def test_spent_since_zero_when_no_rows(ledger):
    store, _, state = ledger
    state["fetch"] = (0.0,)
    assert store.spent_since(datetime.now(UTC)) == 0.0


def test_spent_since_returns_zero_on_db_error(ledger, monkeypatch):
    store, _, _ = ledger
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("conn down")))
    assert store.spent_since(datetime.now(UTC)) == 0.0


# ---------------------------------------------------------------------------
# record (sentinel-row UPSERT)
# ---------------------------------------------------------------------------

def test_record_upserts_against_sentinel_issue(ledger):
    store, calls, _ = ledger
    store.record(amount_usd=1.23)
    sql, params = calls[0]
    assert "INSERT INTO spec_generator_runs" in sql
    assert "ON CONFLICT (issue_number) DO UPDATE" in sql
    assert params[0] == PostgresCostLedger.SENTINEL_ISSUE
    assert params[5] == 1.23


def test_record_swallows_db_errors(ledger, monkeypatch):
    store, _, _ = ledger
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    # Must not raise.
    store.record(amount_usd=2.0)


# ---------------------------------------------------------------------------
# build_budget composition-root helper
# ---------------------------------------------------------------------------

def test_build_budget_without_dsn_returns_none():
    assert build_budget({}) is None


def test_build_budget_with_blank_dsn_returns_none():
    assert build_budget({"CONTROL_PLANE_PG_DSN": "  "}) is None


def test_build_budget_with_dsn_returns_budget():
    pytest.importorskip("psycopg")
    b = build_budget({"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"})
    assert isinstance(b, Budget)
    assert b.weekly_cap_usd == DEFAULT_WEEKLY_CAP_USD


def test_build_budget_honours_custom_cap_env():
    pytest.importorskip("psycopg")
    b = build_budget({
        "CONTROL_PLANE_PG_DSN": "postgresql://x@/y",
        "SPEC_GEN_WEEKLY_CAP_USD": "100",
    })
    assert isinstance(b, Budget)
    assert b.weekly_cap_usd == 100.0


def test_budget_decide_refuses_when_over_cap():
    """End-to-end with a fake ledger: Budget decides refuse when spent >= cap."""
    pytest.importorskip("psycopg")
    b = build_budget({"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"})
    assert b is not None
    # Swap in an in-memory ledger so the test is fully offline.
    from agents.spec_generator.cost import InMemoryCostLedger
    b.ledger = InMemoryCostLedger()
    b.ledger.record(amount_usd=51.0)
    decision = b.decide()
    assert not decision.admit
    assert "refuse-to-draft" in decision.reason


def test_budget_decide_warns_at_eighty_percent():
    pytest.importorskip("psycopg")
    b = build_budget({"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"})
    assert b is not None
    from agents.spec_generator.cost import InMemoryCostLedger
    b.ledger = InMemoryCostLedger()
    b.ledger.record(amount_usd=41.0)  # 82% of 50
    decision = b.decide()
    assert decision.admit
    assert decision.warning is True
