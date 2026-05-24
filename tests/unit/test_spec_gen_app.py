"""Wiring proof for the Spec Generator composition root — `python -m agents.spec_generator`.

Mirrors `tests/unit/test_app.py` for the implementation agent: simulates a
GitHub Action environment + a fixture event file and asserts that
``app.run`` exits cleanly, the rollout gate is consulted, and SHADOW does NOT
fire any real writes (the shadow client records them instead).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents.implementation.observability import EventLog
from agents.implementation.rollout import RolloutDecision
from agents.spec_generator import app


def _write_event(tmp_path: Path, payload: dict[str, Any]) -> Path:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    return event_path


def _base_env(event_path: Path) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(event_path),
        "DATA_PLANE_REPO": "remcaro-rgb/GoliattCo-odoo-custom",
        "ROLLOUT_STAGE": "shadow",
        "OPENCODE_BASE_URL": "http://fake.invalid",
        "GITHUB_RUN_ID": "test-run-1",
    }


@pytest.fixture()
def patched_clients(monkeypatch, fake_client):
    """Replace `build_opencode_client` / `build_github` with offline fakes.

    Keeps the test fully offline — no httpx call, no `gh` invocation.
    """
    from agents.spec_generator import github_io

    fake_client.set_command_result(
        "speckit.specify",
        {"parts": [{"type": "text", "text": "# Spec\n- one\n"}]},
    )
    shadow_github: dict[str, Any] = {}

    def _build_oc(config):
        return fake_client

    def _build_github(repo: str, decision: RolloutDecision):
        # Use an in-memory fake here — ShadowIssueClient wraps it.
        inner = github_io.FakeIssueClient(
            labels_by_issue={42: ["feature-request"]}
        )
        client = github_io.ShadowIssueClient(inner)
        shadow_github["client"] = client
        return client

    monkeypatch.setattr(app, "build_opencode_client", _build_oc)
    monkeypatch.setattr(app, "build_github", _build_github)
    return shadow_github


def test_run_with_no_event_env_returns_zero_and_logs_no_event():
    log = EventLog()
    rc = app.run({}, log=log)
    assert rc == 0
    assert any(r["event"] == "no-event" for r in log.records)


def test_run_aborts_with_missing_data_plane_repo(tmp_path):
    event_path = _write_event(tmp_path, {"action": "opened"})
    log = EventLog()
    rc = app.run(
        {
            "GITHUB_EVENT_NAME": "issues",
            "GITHUB_EVENT_PATH": str(event_path),
            "ROLLOUT_STAGE": "shadow",
        },
        log=log,
    )
    assert rc == 1
    assert any(r["event"] == "misconfigured" for r in log.records)


def test_run_shadow_for_a_feature_request_records_writes_but_posts_nothing(
    tmp_path, patched_clients
):
    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Add CSV export",
            "body": "Please add CSV download.",
            "user": {"login": "alice"},
            "labels": [{"name": "feature-request"}],
        },
    }
    event_path = _write_event(tmp_path, payload)
    log = EventLog()
    rc = app.run(_base_env(event_path), log=log)
    assert rc == 0
    # Rollout decision was logged and was SHADOW.
    rollout_records = [r for r in log.records if r["event"] == "rollout-decision"]
    assert rollout_records
    assert rollout_records[0]["decision"] == str(RolloutDecision.SHADOW)
    # Outcome was "drafted".
    outcome_records = [r for r in log.records if r["event"] == "outcome"]
    assert outcome_records and outcome_records[0]["status"] == "drafted"
    # Shadow client recorded would-be writes — the audit trail SHADOW exists for.
    shadow = patched_clients["client"]
    assert shadow.issue_comments_posted
    assert any(label == "spec-drafted" for _, label in shadow.issue_labels_added)
    # And the run logged each of those would-be writes as `shadow-write`.
    shadow_writes = [r for r in log.records if r["event"] == "shadow-write"]
    assert shadow_writes


def test_run_kill_switch_skip_short_circuits(tmp_path, patched_clients):
    payload = {
        "action": "opened",
        "issue": {
            "number": 99,
            "title": "x",
            "body": "x",
            "user": {"login": "alice"},
            "labels": [],
        },
    }
    event_path = _write_event(tmp_path, payload)
    log = EventLog()
    env = _base_env(event_path) | {"AGENTS_ENABLED": "false"}
    rc = app.run(env, log=log)
    assert rc == 0
    assert any(r["event"] == "skipped" for r in log.records)
    # SKIP must not even build the client / call OpenCode.
    shadow = patched_clients.get("client")
    assert shadow is None or not shadow.issue_comments_posted


def test_rollout_target_from_issue_payload():
    payload = {
        "issue": {"number": 7},
        "repository": {"full_name": "o/r"},
    }
    assert app.rollout_target("issues", payload) == "7"


def test_rollout_target_falls_back_to_repo_when_no_issue():
    payload = {"repository": {"full_name": "o/r"}}
    assert app.rollout_target("ping", payload) == "o/r"
