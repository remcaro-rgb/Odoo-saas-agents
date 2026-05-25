"""HTTP-layer tests — auth + routing. Sentence-Transformers monkeypatched
so the suite stays fast + dependency-free."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """TestClient with a known token + the model boundary patched."""
    monkeypatch.setenv("EMBEDDINGS_SHIM_TOKEN", "test-tok-123")
    from embeddings_shim import main as main_module
    from embeddings_shim import runner as runner_module
    importlib.reload(runner_module)
    importlib.reload(main_module)
    monkeypatch.setattr(
        runner_module, "embed_text",
        lambda text: [0.0] * runner_module.DIMENSIONS if not text else
        [0.1] * runner_module.DIMENSIONS,
    )
    # main.embed_text was bound at import time — re-bind.
    monkeypatch.setattr(main_module, "embed_text", runner_module.embed_text)
    return TestClient(main_module.app)


@pytest.fixture()
def client_misconfigured(monkeypatch):
    """Service started without the token — fails closed."""
    monkeypatch.delenv("EMBEDDINGS_SHIM_TOKEN", raising=False)
    from embeddings_shim import main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app)


def test_root_is_public(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "embeddings-shim"
    assert body["dimensions"] == 384
    assert body["model"].startswith("BAAI/")


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["dimensions"] == 384
    assert body["auth_configured"] is True


def test_embed_requires_token(client):
    r = client.post("/embed", json={"input": "hello"})
    assert r.status_code == 401


def test_embed_rejects_wrong_token(client):
    r = client.post(
        "/embed",
        headers={"Authorization": "Bearer nope"},
        json={"input": "hello"},
    )
    assert r.status_code == 401


def test_embed_returns_dim_384(client):
    r = client.post(
        "/embed",
        headers={"Authorization": "Bearer test-tok-123"},
        json={"input": "hello world"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["embedding"]) == 384


def test_embed_empty_input_zero_vector(client):
    r = client.post(
        "/embed",
        headers={"Authorization": "Bearer test-tok-123"},
        json={"input": ""},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["embedding"] == [0.0] * 384


def test_embed_validates_payload(client):
    # `input` is optional via default="" — passing nothing returns a zero
    # vector, not a 422.
    r = client.post(
        "/embed",
        headers={"Authorization": "Bearer test-tok-123"},
        json={},
    )
    assert r.status_code == 200
    assert len(r.json()["embedding"]) == 384


def test_embed_fails_closed_when_token_unset(client_misconfigured):
    r = client_misconfigured.post(
        "/embed",
        headers={"Authorization": "Bearer anything"},
        json={"input": "hi"},
    )
    assert r.status_code == 503
    assert "misconfigured" in r.json()["detail"]


def test_healthz_reports_auth_off_when_token_unset(client_misconfigured):
    r = client_misconfigured.get("/healthz")
    assert r.status_code == 200
    assert r.json()["auth_configured"] is False
