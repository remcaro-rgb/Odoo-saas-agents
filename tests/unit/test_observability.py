"""Unit tests for the structured event log (Phase E)."""

import json

from agents.implementation.observability import AGENT, EventLog


def test_emit_builds_a_record_with_the_standard_fields():
    log = EventLog(sink=lambda _line: None)
    record = log.emit("plan.done", pr=1501, phase="plan", cost_usd=0.42)
    assert record["agent"] == AGENT
    assert record["event"] == "plan.done"
    assert record["pr"] == 1501
    assert record["phase"] == "plan"
    assert "ts" in record


def test_emit_records_every_event_in_memory():
    log = EventLog(sink=lambda _line: None)
    log.emit("a")
    log.emit("b")
    assert [r["event"] for r in log.records] == ["a", "b"]


def test_emit_writes_one_json_line_to_the_sink():
    lines: list[str] = []
    EventLog(sink=lines.append).emit("plan.done", pr=1501)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "plan.done"
    assert parsed["pr"] == 1501


def test_run_id_is_included_in_every_record_when_set():
    log = EventLog(sink=lambda _line: None, run_id="r_42")
    assert log.emit("x")["run_id"] == "r_42"


def test_emit_opencode_event_re_emits_in_the_project_log_shape():
    log = EventLog(sink=lambda _line: None)
    record = log.emit_opencode_event({"type": "session.idle", "sessionID": "s1"})
    assert record["event"] == "opencode.session.idle"
    assert record["sessionID"] == "s1"
    assert record["agent"] == AGENT
