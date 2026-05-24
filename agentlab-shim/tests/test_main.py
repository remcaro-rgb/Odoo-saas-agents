"""HTTP-layer tests — auth + routing for the FastAPI shim."""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """Build a TestClient with a known-good token set in the env."""
    monkeypatch.setenv("AGENTLAB_SHIM_TOKEN", "test-tok-123")
    # Reload `main` so it picks up the env at import time.
    from agentlab_shim import main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app), main_module


@pytest.fixture()
def client_misconfigured(monkeypatch):
    """A client where AGENTLAB_SHIM_TOKEN is unset — fails closed."""
    monkeypatch.delenv("AGENTLAB_SHIM_TOKEN", raising=False)
    from agentlab_shim import main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app)


def test_healthz_is_public(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["auth_configured"] is True


def test_repro_requires_bearer_token(client):
    c, _ = client
    r = c.post(
        "/repro",
        json={"issue": 1, "title": "x", "body": "x", "attachments": [], "reporter": "alice"},
    )
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]


def test_repro_rejects_wrong_token(client):
    c, _ = client
    r = c.post(
        "/repro",
        headers={"Authorization": "Bearer nope"},
        json={"issue": 1, "title": "x", "body": "x", "attachments": [], "reporter": "alice"},
    )
    assert r.status_code == 401


def test_repro_passes_with_correct_token(client, monkeypatch):
    c, main_module = client
    # Pin runner.run_repro so this test doesn't try to spawn Chromium.
    from agentlab_shim import runner as runner_module
    from agentlab_shim.runner import ReproResponse
    monkeypatch.setattr(
        runner_module, "run_repro",
        lambda req: ReproResponse(outcome="needs_repro_info", summary="stub"),
    )
    r = c.post(
        "/repro",
        headers={"Authorization": "Bearer test-tok-123"},
        json={"issue": 1, "title": "x", "body": "no steps", "attachments": [], "reporter": "a"},
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "needs_repro_info"


def test_repro_fails_closed_when_token_unset(client_misconfigured):
    r = client_misconfigured.post(
        "/repro",
        headers={"Authorization": "Bearer anything"},
        json={"issue": 1, "title": "x", "body": "x", "attachments": [], "reporter": "a"},
    )
    assert r.status_code == 503  # explicit fail-closed
    assert "misconfigured" in r.json()["detail"]


def test_healthz_reports_auth_off_when_token_unset(client_misconfigured):
    r = client_misconfigured.get("/healthz")
    assert r.status_code == 200
    assert r.json()["auth_configured"] is False


def test_repro_validates_payload_shape(client):
    c, _ = client
    # Missing required `issue` -> FastAPI 422.
    r = c.post(
        "/repro",
        headers={"Authorization": "Bearer test-tok-123"},
        json={"title": "x"},
    )
    assert r.status_code == 422
