"""Pre-flight + step-parser + classifier tests for the shim's runner.

Playwright itself is never invoked — the `execute_steps` boundary is the
seam; tests monkeypatch it to drive every classification branch.
"""

from __future__ import annotations

import pytest

from agentlab_shim.runner import (
    ReproRequest,
    _classify_run,
    _StepLog,
    parse_steps,
    pre_flight,
    run_repro,
)
from agentlab_shim import runner as runner_module


def _req(body: str = "", title: str = "") -> ReproRequest:
    return ReproRequest(
        issue=1, title=title, body=body,
        attachments=(), reporter="alice",
        base_url="https://agentlab.test",
    )


# ---------------------------------------------------------------------------
# pre_flight
# ---------------------------------------------------------------------------

def test_pre_flight_blocks_when_no_steps():
    r = pre_flight(_req(body="It crashed. Please fix."))
    assert r is not None
    assert r.outcome == "needs_repro_info"
    assert r.questions


def test_pre_flight_blocks_on_tenant_reference():
    r = pre_flight(_req(body="goto: /web\nclick: x\ntenant id 17 broke"))
    assert r is not None
    assert r.outcome == "needs_fixture"


def test_pre_flight_blocks_on_customer_name():
    r = pre_flight(_req(body="goto: /web\nclick: x\nAcme Corp data leaks"))
    assert r is not None
    assert r.outcome == "needs_fixture"


def test_pre_flight_passes_on_structured_steps():
    body = (
        "Steps:\n"
        "1. goto: /web/login\n"
        "2. fill: input[name=login] = admin\n"
        "3. click: button[type=submit]\n"
        "Expected: dashboard\nActual: 500"
    )
    assert pre_flight(_req(body=body)) is None


# ---------------------------------------------------------------------------
# parse_steps
# ---------------------------------------------------------------------------

def test_parse_steps_extracts_each_primitive():
    body = (
        "goto: /web/login\n"
        "fill: input[name=login] = admin\n"
        "click: button[type=submit]\n"
        "wait: .o_navbar\n"
        "Expected: dashboard"
    )
    parsed = parse_steps(body)
    ops = [op for op, _ in parsed.primitives]
    assert ops == ["goto", "fill", "click", "wait"]
    assert parsed.expected == "dashboard"


def test_parse_steps_handles_case_insensitive_keywords():
    parsed = parse_steps("GoTo: /a\nFILL: x = y")
    assert parsed.primitives[0] == ("goto", ("/a",))
    assert parsed.primitives[1] == ("fill", ("x", "y"))


def test_parse_steps_empty_body_yields_no_primitives():
    parsed = parse_steps("")
    assert parsed.primitives == []
    assert parsed.expected == ""


# ---------------------------------------------------------------------------
# _classify_run — drives the run-outcome branches without Playwright
# ---------------------------------------------------------------------------

def _good_logs(n: int = 2) -> list[_StepLog]:
    return [_StepLog("goto", "/x", ok=True) for _ in range(n)]


def test_classify_repro_confirmed_on_mixed_steps():
    parsed = parse_steps("goto: /a\nclick: b\nExpected: ok")
    logs = [
        _StepLog("goto", "/a", ok=True),
        _StepLog("click", "b", ok=False, detail="timeout"),
    ]
    r = _classify_run(parsed, logs, screenshots=["base64"], final_html="error")
    assert r.outcome == "repro_confirmed"


def test_classify_unavailable_when_every_step_failed():
    parsed = parse_steps("goto: /a\nclick: b")
    logs = [
        _StepLog("goto", "/a", ok=False, detail="net::"),
        _StepLog("click", "b", ok=False, detail="net::"),
    ]
    r = _classify_run(parsed, logs, [], None)
    assert r.outcome == "agentlab_unavailable"


def test_classify_needs_info_when_expected_appeared():
    parsed = parse_steps("goto: /a\nExpected: dashboard")
    r = _classify_run(
        parsed, _good_logs(1),
        screenshots=[], final_html="<body>dashboard renders here</body>",
    )
    assert r.outcome == "needs_repro_info"
    assert r.questions


def test_classify_repro_confirmed_when_expected_absent():
    parsed = parse_steps("goto: /a\nExpected: dashboard")
    r = _classify_run(
        parsed, _good_logs(1),
        screenshots=[], final_html="<body>500 internal server error</body>",
    )
    assert r.outcome == "repro_confirmed"


# ---------------------------------------------------------------------------
# run_repro — full orchestrator, Playwright boundary monkeypatched
# ---------------------------------------------------------------------------

def test_run_repro_short_circuits_on_no_steps():
    response = run_repro(_req(body="nothing structured here"))
    assert response.outcome == "needs_repro_info"


def test_run_repro_short_circuits_on_fixture_data():
    response = run_repro(_req(body="goto: /web\ntenant id 7 broke"))
    assert response.outcome == "needs_fixture"


def test_run_repro_uses_execute_steps_when_pre_flight_passes(monkeypatch):
    body = (
        "Steps:\n1. goto: /web/login\n2. click: button[type=submit]\n"
        "Expected: dashboard\nActual: 500"
    )
    def _fake_execute(parsed, *, base_url, timeout_ms):
        return [_StepLog("goto", "/web/login", ok=True),
                _StepLog("click", "button[type=submit]", ok=True)], ["b64"], "<body>500</body>"

    monkeypatch.setattr(runner_module, "execute_steps", _fake_execute)
    response = run_repro(_req(body=body))
    assert response.outcome == "repro_confirmed"
    assert response.screenshots == ["b64"]
    assert response.logs


def test_run_repro_catches_playwright_exception(monkeypatch):
    body = "Steps:\n1. goto: /a"

    def _boom(*a, **kw):
        raise RuntimeError("playwright crashed")

    monkeypatch.setattr(runner_module, "execute_steps", _boom)
    response = run_repro(_req(body=body))
    assert response.outcome == "agentlab_unavailable"
    assert "playwright crashed" in response.summary


def test_run_repro_needs_info_when_no_primitives_parsed(monkeypatch):
    # Pre-flight passes (numbered list satisfies the marker regex), but the
    # body lacks our concrete primitives — we surface the ambiguity.
    body = "Steps:\n1. open the browser\n2. do the thing"
    response = run_repro(_req(body=body))
    assert response.outcome == "needs_repro_info"


# ---------------------------------------------------------------------------
# Smoke: importing runner_module doesn't pull Playwright as a hard dep
# ---------------------------------------------------------------------------

def test_runner_module_imports_without_playwright_installed():
    # `playwright` is imported only inside `execute_steps`; the module
    # itself must load even when the package isn't installed.
    import importlib
    importlib.reload(runner_module)
    assert hasattr(runner_module, "run_repro")
