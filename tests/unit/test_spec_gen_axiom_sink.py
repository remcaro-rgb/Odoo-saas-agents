"""Unit tests for the Axiom HTTP sink (Phase 1.2 observability)."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from agents.implementation.observability import EventLog
from agents.spec_generator.axiom_sink import (
    AXIOM_API_BASE,
    AxiomSink,
    maybe_build_axiom_sink,
    stdout_and_axiom,
)


class _FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_sink_buffers_lines_does_not_flush_on_each_call():
    sink = AxiomSink(token="t", dataset="d")
    sink("line1")
    sink("line2")
    assert sink.buffer == ["line1", "line2"]


def test_flush_posts_ndjson_to_axiom_ingest_endpoint():
    captured: dict[str, Any] = {}

    def _open(req, *a, **kw):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["auth"] = req.get_header("Authorization")
        captured["ctype"] = req.get_header("Content-type")
        return _FakeResp(status=200)

    sink = AxiomSink(token="tok-xyz", dataset="spec-generator", http_open=_open)
    sink('{"event":"a"}')
    sink('{"event":"b"}')
    ok = sink.flush()
    assert ok is True
    assert captured["method"] == "POST"
    assert captured["url"] == f"{AXIOM_API_BASE}/v1/datasets/spec-generator/ingest"
    assert captured["auth"] == "Bearer tok-xyz"
    assert captured["ctype"] == "application/x-ndjson"
    assert captured["body"] == b'{"event":"a"}\n{"event":"b"}'
    # Buffer is cleared on success so a second flush is a no-op.
    assert sink.buffer == []
    assert sink.flush() is True


def test_flush_with_no_records_is_a_noop():
    sink = AxiomSink(
        token="t",
        dataset="d",
        http_open=lambda *a, **k: pytest.fail("should not call HTTP"),
    )
    assert sink.flush() is True


def test_http_failure_is_swallowed_and_records_dropped(capsys):
    def _boom(*a, **k):
        raise URLError("connection refused")

    sink = AxiomSink(token="t", dataset="d", http_open=_boom)
    sink("rec1")
    ok = sink.flush()
    assert ok is False
    assert sink.buffer == []  # cleared even on failure
    captured = capsys.readouterr()
    assert "axiom-sink: flush failed" in captured.err


def test_non_2xx_status_is_swallowed(capsys):
    sink = AxiomSink(
        token="t", dataset="d", http_open=lambda *a, **k: _FakeResp(status=500)
    )
    sink("rec1")
    ok = sink.flush()
    assert ok is False
    assert "ingest returned HTTP 500" in capsys.readouterr().err


def test_buffer_overflow_drops_records(capsys):
    sink = AxiomSink(token="t", dataset="d", http_open=lambda *a, **k: _FakeResp())
    for i in range(AxiomSink.MAX_RECORDS + 5):
        sink(f"record-{i}")
    assert len(sink.buffer) == AxiomSink.MAX_RECORDS
    sink.flush()
    err = capsys.readouterr().err
    assert "5 record(s) dropped" in err


def test_stdout_and_axiom_composite_forwards_to_both(capsys):
    sink = stdout_and_axiom(token="t", dataset="d")
    sink('{"event":"x"}')
    out = capsys.readouterr().out
    assert '{"event":"x"}' in out
    axiom = sink.axiom  # type: ignore[attr-defined]
    assert axiom.buffer == ['{"event":"x"}']


def test_eventlog_emit_uses_axiom_sink_when_configured():
    """EventLog with a composite sink writes to both stdout and the buffer."""
    sink = stdout_and_axiom(token="t", dataset="d")
    log = EventLog(sink=sink, run_id="r1")
    log.emit("outcome", status="drafted", issue=42)
    axiom = sink.axiom  # type: ignore[attr-defined]
    assert len(axiom.buffer) == 1
    record = json.loads(axiom.buffer[0])
    assert record["event"] == "outcome"
    assert record["status"] == "drafted"
    assert record["issue"] == 42
    assert record["run_id"] == "r1"


def test_maybe_build_returns_none_without_env():
    assert maybe_build_axiom_sink({}) is None
    assert maybe_build_axiom_sink({"AXIOM_TOKEN": "x"}) is None  # no dataset
    assert maybe_build_axiom_sink({"AXIOM_DATASET": "x"}) is None  # no token


def test_maybe_build_returns_composite_when_env_set():
    sink = maybe_build_axiom_sink({"AXIOM_TOKEN": "t", "AXIOM_DATASET": "d"})
    assert sink is not None
    assert hasattr(sink, "axiom")


def test_404_unknown_dataset_surfaces_in_stderr(capsys):
    """A common misconfig: token valid but dataset name wrong → HTTP 404.

    Axiom uses HTTPError (urllib) for non-2xx; treat it as the URLError
    base class so the warning shows the same shape as a network failure.
    """
    def _open(req, *a, **k):
        raise HTTPError(req.full_url, 404, "dataset not found", {}, io.BytesIO(b""))

    sink = AxiomSink(token="t", dataset="missing", http_open=_open)
    sink('{"event":"x"}')
    ok = sink.flush()
    assert ok is False
    err = capsys.readouterr().err
    assert "axiom-sink: flush failed" in err
