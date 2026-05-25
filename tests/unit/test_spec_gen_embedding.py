"""Unit tests for embedding.py — OpenAI client + fake."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from agents.spec_generator import embedding as emb_module
from agents.spec_generator.embedding import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    FakeEmbeddingClient,
    LocalEmbeddingClient,
    OpenAIEmbeddingClient,
    build_embedding_client,
    cosine_similarity,
)

# ---------------------------------------------------------------------------
# FakeEmbeddingClient
# ---------------------------------------------------------------------------

def test_fake_embedding_client_returns_correct_dimensions():
    vec = FakeEmbeddingClient().embed("hello")
    assert len(vec) == DEFAULT_DIMENSIONS
    assert all(-1.0 <= x <= 1.0 for x in vec)


def test_fake_embedding_client_is_deterministic():
    a = FakeEmbeddingClient().embed("same input")
    b = FakeEmbeddingClient().embed("same input")
    assert a == b


def test_fake_embedding_client_different_inputs_diverge():
    a = FakeEmbeddingClient().embed("foo")
    b = FakeEmbeddingClient().embed("bar")
    assert a != b


def test_fake_embedding_client_custom_dim():
    vec = FakeEmbeddingClient(dimensions=8).embed("anything")
    assert len(vec) == 8


# ---------------------------------------------------------------------------
# OpenAIEmbeddingClient — urlopen monkeypatched
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _ok_payload(dim: int = DEFAULT_DIMENSIONS) -> bytes:
    return json.dumps({
        "data": [{"embedding": [0.001] * dim, "index": 0}],
        "model": DEFAULT_MODEL,
    }).encode()


def test_openai_client_sends_expected_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["auth"] = req.headers.get("Authorization")
        return _Resp(_ok_payload())

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = OpenAIEmbeddingClient(api_key="sk-test-xyz")
    vec = client.embed("hello world")
    assert len(vec) == DEFAULT_DIMENSIONS
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    body = captured["body"]
    assert body["model"] == DEFAULT_MODEL
    assert body["input"] == "hello world"
    assert body["dimensions"] == DEFAULT_DIMENSIONS
    assert captured["auth"] == "Bearer sk-test-xyz"


def test_openai_client_short_circuits_on_empty_input(monkeypatch):
    """No HTTP call for empty/whitespace input."""
    called: list[bool] = []
    def _urlopen(*a, **k):  # pragma: no cover - we assert it's NOT called
        called.append(True)
        raise AssertionError("should not call OpenAI")
    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = OpenAIEmbeddingClient(api_key="x")
    assert client.embed("") == [0.0] * DEFAULT_DIMENSIONS
    assert client.embed("   ") == [0.0] * DEFAULT_DIMENSIONS
    assert called == []


def test_openai_client_retries_429_then_succeeds(monkeypatch):
    calls: list[int] = [0]

    def _urlopen(req, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise HTTPError(
                req.full_url, 429, "rate limited", {},
                io.BytesIO(b'{"error":"slow"}'),
            )
        return _Resp(_ok_payload())

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(emb_module.time, "sleep", lambda *_: None)
    client = OpenAIEmbeddingClient(api_key="x", max_retries=3)
    vec = client.embed("text")
    assert len(vec) == DEFAULT_DIMENSIONS
    assert calls[0] == 2


def test_openai_client_raises_on_4xx_non_429(monkeypatch):
    def _urlopen(req, *args, **kwargs):
        raise HTTPError(
            req.full_url, 401, "unauthorized", {},
            io.BytesIO(b'{"error":"bad token"}'),
        )

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = OpenAIEmbeddingClient(api_key="bad")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.embed("text")


def test_openai_client_raises_on_dim_mismatch(monkeypatch):
    bad_body = json.dumps({"data": [{"embedding": [0.0] * 64, "index": 0}]}).encode()
    monkeypatch.setattr(
        emb_module.urllib.request, "urlopen",
        lambda *a, **k: _Resp(bad_body),
    )
    client = OpenAIEmbeddingClient(api_key="x")
    with pytest.raises(RuntimeError, match=f"{DEFAULT_DIMENSIONS}-d vector, got 64"):
        client.embed("text")


def test_openai_client_raises_on_empty_data(monkeypatch):
    monkeypatch.setattr(
        emb_module.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps({"data": []}).encode()),
    )
    client = OpenAIEmbeddingClient(api_key="x")
    with pytest.raises(RuntimeError, match="no embeddings"):
        client.embed("text")


# ---------------------------------------------------------------------------
# LocalEmbeddingClient — talks to the self-hosted shim
# ---------------------------------------------------------------------------

def test_local_client_sends_expected_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["auth"] = req.headers.get("Authorization")
        return _Resp(json.dumps({"embedding": [0.001] * DEFAULT_DIMENSIONS}).encode())

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = LocalEmbeddingClient(
        base_url="https://shim.example", token="shim-tok-xyz",
    )
    vec = client.embed("hello world")
    assert len(vec) == DEFAULT_DIMENSIONS
    assert captured["url"] == "https://shim.example/embed"
    assert captured["body"] == {"input": "hello world"}
    assert captured["auth"] == "Bearer shim-tok-xyz"


def test_local_client_short_circuits_on_empty_input(monkeypatch):
    def _urlopen(*a, **k):
        raise AssertionError("should not call shim")
    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = LocalEmbeddingClient(base_url="https://x", token="t")
    assert client.embed("") == [0.0] * DEFAULT_DIMENSIONS
    assert client.embed("   ") == [0.0] * DEFAULT_DIMENSIONS


def test_local_client_retries_on_5xx(monkeypatch):
    calls: list[int] = [0]

    def _urlopen(req, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise HTTPError(
                req.full_url, 503, "down", {},
                io.BytesIO(b'{"error":"warming up"}'),
            )
        return _Resp(json.dumps({"embedding": [0.0] * DEFAULT_DIMENSIONS}).encode())

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(emb_module.time, "sleep", lambda *_: None)
    client = LocalEmbeddingClient(base_url="https://x", token="t", max_retries=3)
    vec = client.embed("text")
    assert len(vec) == DEFAULT_DIMENSIONS
    assert calls[0] == 2


def test_local_client_raises_on_401(monkeypatch):
    def _urlopen(req, *args, **kwargs):
        raise HTTPError(
            req.full_url, 401, "unauthorized", {},
            io.BytesIO(b'{"error":"bad token"}'),
        )

    monkeypatch.setattr(emb_module.urllib.request, "urlopen", _urlopen)
    client = LocalEmbeddingClient(base_url="https://x", token="bad")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.embed("text")


def test_local_client_raises_on_dim_mismatch_with_helpful_message(monkeypatch):
    bad_body = json.dumps({"embedding": [0.0] * 768}).encode()
    monkeypatch.setattr(
        emb_module.urllib.request, "urlopen",
        lambda *a, **k: _Resp(bad_body),
    )
    client = LocalEmbeddingClient(base_url="https://x", token="t")
    with pytest.raises(RuntimeError, match="Model swap"):
        client.embed("text")


# ---------------------------------------------------------------------------
# build_embedding_client / cosine_similarity
# ---------------------------------------------------------------------------

def test_build_embedding_client_unset_returns_none():
    assert build_embedding_client({}) is None
    assert build_embedding_client({"OPENAI_API_KEY": "   "}) is None


def test_build_embedding_client_prefers_shim_when_both_set():
    client = build_embedding_client({
        "EMBEDDINGS_SHIM_URL": "https://shim.example",
        "EMBEDDINGS_SHIM_TOKEN": "tok",
        "OPENAI_API_KEY": "sk-xyz",
    })
    assert isinstance(client, LocalEmbeddingClient)
    assert client.base_url == "https://shim.example"


def test_build_embedding_client_falls_back_to_openai_when_only_key_set():
    client = build_embedding_client({"OPENAI_API_KEY": "sk-xyz"})
    assert isinstance(client, OpenAIEmbeddingClient)
    assert client.api_key == "sk-xyz"


def test_build_embedding_client_shim_needs_both_url_and_token():
    # URL without token -> doesn't use shim, falls through to None
    assert build_embedding_client({"EMBEDDINGS_SHIM_URL": "https://x"}) is None
    # Token without URL -> same
    assert build_embedding_client({"EMBEDDINGS_SHIM_TOKEN": "tok"}) is None


def test_cosine_similarity_identical():
    v = [1.0, 0.0, 0.0]
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_dim_mismatch_raises():
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_cosine_similarity_zero_vectors_returns_zero():
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
