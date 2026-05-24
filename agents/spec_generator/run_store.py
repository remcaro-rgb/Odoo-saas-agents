"""Per-issue state persistence (Tier 3, design spec §6).

The ``spec_generator_runs`` Postgres table holds one row per source issue
the agent acts on, mutated at every phase transition. This module is the
agent-side adapter:

- ``RunStore`` — Protocol covering both session-id storage (the Tier 2
  ``SessionStore`` surface) and the new phase-transition writes.
- ``PostgresRunStore`` — production implementation against the
  control-plane Postgres. Uses ``psycopg`` v3 with a connection per call
  (one short-lived transaction per write — acceptable for the agent's
  volume; reuse-pool can ship later).
- ``NoOpRunStore`` — used when ``CONTROL_PLANE_PG_DSN`` is unset (SHADOW,
  local dev, or any deployment that hasn't applied the migration).
  ``record_*`` calls are silently dropped; ``get`` / ``set`` / ``delete``
  delegate to an in-memory or JSON-file fallback session store.
- ``InMemoryRunStore`` — for unit tests; exposes the recorded calls.

Schema reference: ``migrations/2026-05-24-spec-generator-runs.sql``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .session_store import InMemorySessionStore, SessionStore

# Phase vocabulary — kept in sync with the SQL column's `phase` field.
PHASE_DRAFTED = "drafted"
PHASE_AWAITING_REPORTER_CONFIRM = "awaiting_reporter_confirm"
PHASE_INTENT_CONFIRMED = "intent_confirmed"
PHASE_COMPLETED = "completed"
PHASE_ESCALATED = "escalated"

# Source vocabulary — `source` column. Surfaces in the dashboard's intake-
# funnel panel (Tier 6).
SOURCE_GITHUB_ISSUE = "github_issue"
SOURCE_CHATBOT = "chatbot"
SOURCE_EMAIL = "email"


@dataclass(frozen=True)
class DraftRecord:
    """Fields a new ``drafted`` row carries."""

    issue: int
    kind: str
    confidence: float
    branch: str
    spec_path: str
    source: str = SOURCE_GITHUB_ISSUE
    opencode_session_id: str | None = None
    metadata: dict[str, Any] | None = None


@runtime_checkable
class RunStore(Protocol):
    """Phase-transition writer + session-id store for a `spec_generator_runs` row."""

    # SessionStore surface (Tier 2) — re-exposed here so callers can use one
    # store instead of two. PostgresRunStore reads/writes the same row's
    # `opencode_session_id` column for these.
    def get(self, pr_or_issue: int) -> str | None: ...
    def set(self, pr_or_issue: int, session_id: str) -> None: ...
    def delete(self, pr_or_issue: int) -> None: ...

    # Tier 3 phase-transition writes.
    def record_draft(self, draft: DraftRecord) -> None:
        """UPSERT the row in `drafted` phase. Idempotent on `issue_number`."""

    def record_pr_opened(self, issue: int, pr: int) -> None:
        """UPDATE pr_number once the spec PR exists. Advances phase to
        `awaiting_reporter_confirm`."""

    def record_phase(self, issue: int, phase: str) -> None:
        """UPDATE phase. Caller decides the right phase from the orchestrator
        outcome (e.g. `escalated`, `intent_confirmed`, `completed`)."""

    def record_reporter_activity(self, issue: int) -> None:
        """Bump `last_reporter_activity_at = now()` — used by the sweep to
        compute the 24h silence threshold."""

    def record_cost(self, issue: int, amount_usd: float) -> None:
        """ADD `amount_usd` to the row's `cost_usd` total. Feeds the spend
        cap (Tier 6) and the dashboard's spend panels."""


class NoOpRunStore:
    """Silently-dropped phase writes + a session store delegate.

    Used when ``CONTROL_PLANE_PG_DSN`` is unset — SHADOW runs, local dev,
    and any deployment that hasn't applied the migration yet. The
    delegate handles the session-id contract (so iterate workflows still
    find the OpenCode session); phase writes are no-ops.
    """

    def __init__(self, sessions: SessionStore | None = None) -> None:
        self._sessions: SessionStore = sessions or InMemorySessionStore()

    def get(self, pr_or_issue: int) -> str | None:
        return self._sessions.get(pr_or_issue)

    def set(self, pr_or_issue: int, session_id: str) -> None:
        self._sessions.set(pr_or_issue, session_id)

    def delete(self, pr_or_issue: int) -> None:
        self._sessions.delete(pr_or_issue)

    def record_draft(self, draft: DraftRecord) -> None: ...
    def record_pr_opened(self, issue: int, pr: int) -> None: ...
    def record_phase(self, issue: int, phase: str) -> None: ...
    def record_reporter_activity(self, issue: int) -> None: ...
    def record_cost(self, issue: int, amount_usd: float) -> None: ...


class InMemoryRunStore:
    """Records phase transitions in memory — unit-test fixture.

    Exposes the recorded calls so tests can assert the agent walked the
    expected state machine.
    """

    def __init__(self) -> None:
        self.drafts: list[DraftRecord] = []
        self.prs_opened: list[tuple[int, int]] = []
        self.phases: list[tuple[int, str]] = []
        self.reporter_activity: list[int] = []
        self.costs: list[tuple[int, float]] = []
        self._sessions: dict[int, str] = {}

    def get(self, pr_or_issue: int) -> str | None:
        return self._sessions.get(int(pr_or_issue))

    def set(self, pr_or_issue: int, session_id: str) -> None:
        self._sessions[int(pr_or_issue)] = session_id

    def delete(self, pr_or_issue: int) -> None:
        self._sessions.pop(int(pr_or_issue), None)

    def record_draft(self, draft: DraftRecord) -> None:
        self.drafts.append(draft)

    def record_pr_opened(self, issue: int, pr: int) -> None:
        self.prs_opened.append((int(issue), int(pr)))

    def record_phase(self, issue: int, phase: str) -> None:
        self.phases.append((int(issue), phase))

    def record_reporter_activity(self, issue: int) -> None:
        self.reporter_activity.append(int(issue))

    def record_cost(self, issue: int, amount_usd: float) -> None:
        self.costs.append((int(issue), float(amount_usd)))


class PostgresRunStore:
    """Production `RunStore` backed by the control-plane Postgres.

    Uses ``psycopg`` v3 — autocommit, one short-lived connection per call.
    The volume is low (a handful of writes per agent run) so a connection
    pool isn't worth the dependency yet. Every write is wrapped in a
    try/except that LOGS but does not raise — a Postgres outage must not
    crash an agent run that's otherwise succeeding (the row is convenience,
    not correctness).
    """

    def __init__(
        self, dsn: str, *, sessions_fallback: SessionStore | None = None
    ) -> None:
        # Import lazily — `psycopg` is an optional dep so SHADOW envs that
        # never construct a PostgresRunStore can skip the install.
        import psycopg  # noqa: F401  (re-imported inside each method)

        self.dsn = dsn
        # Use a session-store fallback for `get/set/delete` only if the DB
        # read fails (defence in depth — iterate workflow still functions
        # if Postgres is briefly unreachable).
        self._sessions_fallback: SessionStore = (
            sessions_fallback or InMemorySessionStore()
        )

    def _connect(self) -> Any:
        import psycopg  # type: ignore[import-not-found]

        return psycopg.connect(self.dsn, autocommit=True)

    # -- session-id surface ---------------------------------------------------

    def get(self, pr_or_issue: int) -> str | None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT opencode_session_id FROM spec_generator_runs "
                    "WHERE issue_number = %s",
                    (int(pr_or_issue),),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
        except Exception as exc:  # noqa: BLE001
            _log_warning(f"PostgresRunStore.get failed: {exc}")
        return self._sessions_fallback.get(pr_or_issue)

    def set(self, pr_or_issue: int, session_id: str) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE spec_generator_runs SET opencode_session_id = %s "
                    "WHERE issue_number = %s",
                    (session_id, int(pr_or_issue)),
                )
                # A 0-row UPDATE means the row hasn't been INSERTed yet
                # (the draft path will INSERT it shortly). We mirror to the
                # fallback so the next `get` in the same process succeeds.
                if cur.rowcount == 0:
                    self._sessions_fallback.set(pr_or_issue, session_id)
        except Exception as exc:  # noqa: BLE001
            _log_warning(f"PostgresRunStore.set failed: {exc}")
            self._sessions_fallback.set(pr_or_issue, session_id)

    def delete(self, pr_or_issue: int) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE spec_generator_runs SET opencode_session_id = NULL "
                    "WHERE issue_number = %s",
                    (int(pr_or_issue),),
                )
        except Exception as exc:  # noqa: BLE001
            _log_warning(f"PostgresRunStore.delete failed: {exc}")
        self._sessions_fallback.delete(pr_or_issue)

    # -- phase-transition surface --------------------------------------------

    def record_draft(self, draft: DraftRecord) -> None:
        sql = (
            "INSERT INTO spec_generator_runs ("
            "  issue_number, kind, confidence, source, opencode_session_id,"
            "  branch, spec_path, phase, metadata"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
            " ON CONFLICT (issue_number) DO UPDATE SET"
            "  kind = EXCLUDED.kind,"
            "  confidence = EXCLUDED.confidence,"
            "  source = EXCLUDED.source,"
            "  opencode_session_id = COALESCE("
            "    EXCLUDED.opencode_session_id,"
            "    spec_generator_runs.opencode_session_id),"
            "  branch = EXCLUDED.branch,"
            "  spec_path = EXCLUDED.spec_path,"
            "  phase = EXCLUDED.phase,"
            "  metadata = spec_generator_runs.metadata || EXCLUDED.metadata"
        )
        metadata_json = json.dumps(draft.metadata or {})
        params = (
            draft.issue,
            draft.kind,
            draft.confidence,
            draft.source,
            draft.opencode_session_id,
            draft.branch,
            draft.spec_path,
            PHASE_DRAFTED,
            metadata_json,
        )
        self._exec(sql, params, "record_draft")

    def record_pr_opened(self, issue: int, pr: int) -> None:
        self._exec(
            "UPDATE spec_generator_runs SET pr_number = %s, phase = %s "
            "WHERE issue_number = %s",
            (int(pr), PHASE_AWAITING_REPORTER_CONFIRM, int(issue)),
            "record_pr_opened",
        )

    def record_phase(self, issue: int, phase: str) -> None:
        self._exec(
            "UPDATE spec_generator_runs SET phase = %s WHERE issue_number = %s",
            (phase, int(issue)),
            "record_phase",
        )

    def record_reporter_activity(self, issue: int) -> None:
        self._exec(
            "UPDATE spec_generator_runs "
            "SET last_reporter_activity_at = NOW() "
            "WHERE issue_number = %s",
            (int(issue),),
            "record_reporter_activity",
        )

    def record_cost(self, issue: int, amount_usd: float) -> None:
        self._exec(
            "UPDATE spec_generator_runs "
            "SET cost_usd = cost_usd + %s WHERE issue_number = %s",
            (float(amount_usd), int(issue)),
            "record_cost",
        )

    # -- internals -----------------------------------------------------------

    def _exec(self, sql: str, params: tuple[Any, ...], label: str) -> None:
        """Run a write — log + swallow on failure (the agent run wins)."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            _log_warning(f"PostgresRunStore.{label} failed: {exc}")


def _log_warning(msg: str) -> None:
    """Write to stderr (the EventLog sink picks it up via the workflow logs).

    Kept as a free function so it can be monkeypatched in tests; the agent
    deliberately does not raise on a control-plane outage — losing a row is
    acceptable, losing the user-visible spec PR is not.
    """
    import sys

    print(f"warning: {msg}", file=sys.stderr, flush=True)


def build_run_store(env: dict[str, str], *, sessions: SessionStore) -> RunStore:
    """Composition-root helper. Reads ``CONTROL_PLANE_PG_DSN`` from env.

    Unset (or empty) -> ``NoOpRunStore(sessions)`` so SHADOW + local dev
    keep working without Postgres. Set -> ``PostgresRunStore`` with the
    same session store as a fallback for transient connection failures.
    """
    dsn = (env.get("CONTROL_PLANE_PG_DSN") or "").strip()
    if not dsn:
        return NoOpRunStore(sessions=sessions)
    try:
        return PostgresRunStore(dsn, sessions_fallback=sessions)
    except ImportError:
        # `psycopg` is an optional dep. Fall back gracefully so a bad
        # install doesn't kill the agent (the EventLog records the warning
        # via _log_warning + the sink picks it up).
        _log_warning(
            "CONTROL_PLANE_PG_DSN set but psycopg is not installed; "
            "falling back to NoOpRunStore"
        )
        return NoOpRunStore(sessions=sessions)
