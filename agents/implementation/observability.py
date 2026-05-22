"""Structured observability — JSON event logs (Phase E, design §14).

Every run emits structured JSON records in one shape, whatever the sink. The
alternative plan drops the bespoke Logger-port + adapter matrix: OpenCode already
emits rich typed SSE events, so `EventLog` just re-emits them — and the
orchestrator's own milestones — as project-shaped JSON (`{ts, agent, event, ...}`).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

#: The `agent` tag on every record (design §14.1).
AGENT = "implementation"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stdout_sink(line: str) -> None:
    print(line, file=sys.stdout, flush=True)


class EventLog:
    """Emits structured JSON event records to a sink.

    Every record carries `ts`, `agent`, `event` (and `run_id` when set), plus
    whatever fields the caller passes — `pr`, `phase`, `cost_usd`, `duration_ms`.
    `records` keeps every emitted record in memory: the unit-test surface, and a
    per-run summary source for the dashboard (design §14.2).
    """

    def __init__(
        self,
        *,
        sink: Callable[[str], None] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.records: list[dict[str, Any]] = []
        self.run_id = run_id
        self._sink: Callable[[str], None] = sink or _stdout_sink

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Emit one structured event. Returns the record that was written."""
        record: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "agent": AGENT,
            "event": event,
        }
        if self.run_id is not None:
            record["run_id"] = self.run_id
        record.update(fields)
        self._sink(json.dumps(record, separators=(",", ":"), sort_keys=True))
        self.records.append(record)
        return dict(record)  # a copy — a caller cannot mutate the stored record

    def emit_opencode_event(self, sse_event: dict[str, Any]) -> dict[str, Any]:
        """Re-emit an OpenCode SSE event (from `OpenCodeClient.stream_events`) in
        the project's log shape — the event type becomes `opencode.<type>`."""
        event_type = str(sse_event.get("type", "unknown"))
        fields = {key: value for key, value in sse_event.items() if key != "type"}
        return self.emit(f"opencode.{event_type}", **fields)
