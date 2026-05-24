"""Unit tests for run_store.py (Tier 3 state persistence)."""

from __future__ import annotations

from typing import Any

import pytest

from agents.spec_generator.run_store import (
    PHASE_DRAFTED,
    PHASE_ESCALATED,
    PHASE_INTENT_CONFIRMED,
    DraftRecord,
    InMemoryRunStore,
    NoOpRunStore,
    PostgresRunStore,
    build_run_store,
)
from agents.spec_generator.session_store import InMemorySessionStore


# ---------------------------------------------------------------------------
# InMemoryRunStore — the test fixture itself
# ---------------------------------------------------------------------------

def test_in_memory_run_store_records_draft():
    store = InMemoryRunStore()
    store.record_draft(
        DraftRecord(
            issue=7, kind="feature", confidence=0.9,
            branch="agent/spec-0007-x", spec_path="docs/specs/x.md",
        )
    )
    assert len(store.drafts) == 1
    assert store.drafts[0].issue == 7


def test_in_memory_run_store_session_id_round_trip():
    store = InMemoryRunStore()
    assert store.get(7) is None
    store.set(7, "sess-A")
    assert store.get(7) == "sess-A"
    store.delete(7)
    assert store.get(7) is None


def test_in_memory_records_phase_pr_activity_cost():
    store = InMemoryRunStore()
    store.record_pr_opened(7, 42)
    store.record_phase(7, PHASE_INTENT_CONFIRMED)
    store.record_reporter_activity(7)
    store.record_cost(7, 0.42)
    assert store.prs_opened == [(7, 42)]
    assert store.phases == [(7, PHASE_INTENT_CONFIRMED)]
    assert store.reporter_activity == [7]
    assert store.costs == [(7, 0.42)]


# ---------------------------------------------------------------------------
# NoOpRunStore — the SHADOW / no-DSN fallback
# ---------------------------------------------------------------------------

def test_no_op_run_store_session_passthrough():
    inner = InMemorySessionStore({1: "sess-1"})
    store = NoOpRunStore(sessions=inner)
    assert store.get(1) == "sess-1"
    store.set(2, "sess-2")
    assert inner.get(2) == "sess-2"
    store.delete(2)
    assert inner.get(2) is None


def test_no_op_run_store_phase_writes_silent():
    store = NoOpRunStore()
    # Just confirm none of these raise; they're no-ops.
    store.record_draft(DraftRecord(
        issue=1, kind="feature", confidence=0.9,
        branch="b", spec_path="p",
    ))
    store.record_pr_opened(1, 2)
    store.record_phase(1, PHASE_DRAFTED)
    store.record_reporter_activity(1)
    store.record_cost(1, 0.1)


# ---------------------------------------------------------------------------
# build_run_store — the composition-root helper
# ---------------------------------------------------------------------------

def test_build_run_store_without_dsn_returns_no_op():
    sessions = InMemorySessionStore()
    store = build_run_store({}, sessions=sessions)
    assert isinstance(store, NoOpRunStore)


def test_build_run_store_with_blank_dsn_returns_no_op():
    sessions = InMemorySessionStore()
    store = build_run_store({"CONTROL_PLANE_PG_DSN": "  "}, sessions=sessions)
    assert isinstance(store, NoOpRunStore)


def test_build_run_store_with_dsn_returns_postgres_or_falls_back(monkeypatch):
    """If psycopg is installed we get a PostgresRunStore; if not,
    `build_run_store` falls back to NoOpRunStore with a warning."""
    sessions = InMemorySessionStore()
    try:
        import psycopg  # noqa: F401
        store = build_run_store(
            {"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"}, sessions=sessions
        )
        assert isinstance(store, PostgresRunStore)
    except ImportError:
        store = build_run_store(
            {"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"}, sessions=sessions
        )
        assert isinstance(store, NoOpRunStore)


# ---------------------------------------------------------------------------
# PostgresRunStore — schema validation via psycopg connection mocking
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]],
                 rowcount: int = 1, fetch: tuple[Any, ...] | None = None):
        self._calls = calls
        self.rowcount = rowcount
        self._fetch = fetch

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._calls.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._fetch

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]],
                 rowcount: int = 1, fetch: tuple[Any, ...] | None = None):
        self._calls = calls
        self._rowcount = rowcount
        self._fetch = fetch

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._calls, self._rowcount, self._fetch)

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def postgres_store(monkeypatch):
    """Build a PostgresRunStore against a captured-call fake.

    Returns (store, calls) where calls is appended-to on every execute.
    """
    pytest.importorskip("psycopg")
    calls: list[tuple[str, tuple[Any, ...]]] = []
    state: dict[str, Any] = {"rowcount": 1, "fetch": None}

    def _fake_connect(self):
        return _FakeConn(calls, state["rowcount"], state["fetch"])

    store = PostgresRunStore("postgresql://x@/y")
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(
        calls, state["rowcount"], state["fetch"]
    ))
    return store, calls, state


def test_postgres_record_draft_emits_upsert(postgres_store):
    store, calls, _ = postgres_store
    store.record_draft(DraftRecord(
        issue=7, kind="feature", confidence=0.92,
        branch="agent/spec-0007-x", spec_path="docs/specs/x.md",
        opencode_session_id="sess-1",
        metadata={"open_questions": 2},
    ))
    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO spec_generator_runs" in sql
    assert "ON CONFLICT (issue_number) DO UPDATE" in sql
    assert params[0] == 7
    assert params[1] == "feature"
    assert params[2] == 0.92
    assert params[6] == "docs/specs/x.md"
    assert params[7] == PHASE_DRAFTED


def test_postgres_record_pr_opened_advances_phase(postgres_store):
    store, calls, _ = postgres_store
    store.record_pr_opened(issue=7, pr=42)
    assert len(calls) == 1
    sql, params = calls[0]
    assert "pr_number = %s" in sql
    assert "phase = %s" in sql
    assert params == (42, "awaiting_reporter_confirm", 7)


def test_postgres_record_phase_emits_update(postgres_store):
    store, calls, _ = postgres_store
    store.record_phase(7, PHASE_INTENT_CONFIRMED)
    sql, params = calls[0]
    assert "SET phase = %s" in sql
    assert params == ("intent_confirmed", 7)


def test_postgres_record_reporter_activity_uses_now(postgres_store):
    store, calls, _ = postgres_store
    store.record_reporter_activity(7)
    sql, params = calls[0]
    assert "last_reporter_activity_at = NOW()" in sql
    assert params == (7,)


def test_postgres_record_cost_uses_increment(postgres_store):
    store, calls, _ = postgres_store
    store.record_cost(7, 1.23)
    sql, params = calls[0]
    assert "cost_usd = cost_usd + %s" in sql
    assert params == (1.23, 7)


def test_postgres_get_falls_back_on_zero_rows(postgres_store):
    store, _, state = postgres_store
    state["fetch"] = None  # row not found
    # Seed the fallback so the assertion can find it.
    store._sessions_fallback.set(7, "sess-fb")
    assert store.get(7) == "sess-fb"


def test_postgres_get_prefers_db_value(postgres_store):
    store, _, state = postgres_store
    state["fetch"] = ("sess-from-db",)
    assert store.get(7) == "sess-from-db"


def test_postgres_set_zero_rowcount_mirrors_to_fallback(postgres_store):
    store, _, state = postgres_store
    state["rowcount"] = 0  # the row doesn't exist yet (pre-draft set)
    store.set(7, "sess-X")
    assert store._sessions_fallback.get(7) == "sess-X"


def test_postgres_db_error_does_not_raise(postgres_store, monkeypatch):
    store, _, _ = postgres_store

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(store, "_connect", _boom)
    # All writes must swallow the error so the agent run still succeeds.
    store.record_draft(DraftRecord(
        issue=1, kind="feature", confidence=0.9,
        branch="b", spec_path="p",
    ))
    store.record_pr_opened(1, 2)
    store.record_phase(1, PHASE_ESCALATED)
    store.record_reporter_activity(1)
    store.record_cost(1, 0.1)
    # `get` falls back to the in-memory session store.
    assert store.get(1) is None
