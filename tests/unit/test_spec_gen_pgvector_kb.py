"""Unit tests for pgvector_kb.PgvectorKnowledgeBase — psycopg mocked."""

from __future__ import annotations

from typing import Any

import pytest

from agents.spec_generator.dup_detector import DuplicateCandidate
from agents.spec_generator.embedding import FakeEmbeddingClient
from agents.spec_generator.pgvector_kb import (
    PgvectorKnowledgeBase,
    _vector_literal,
    build_knowledge_base,
)


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, Any]], fetch: list[Any] | None = None):
        self._calls = calls
        self._fetch = fetch or []

    def execute(self, sql: str, params: Any = ()) -> None:
        self._calls.append((sql, params))

    def fetchall(self) -> list[Any]:
        return self._fetch

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, calls: list[tuple[str, Any]], fetch: list[Any] | None = None):
        self._calls = calls
        self._fetch = fetch

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._calls, self._fetch)

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def kb(monkeypatch):
    """A PgvectorKnowledgeBase whose `_connect` returns a captured-call fake."""
    pytest.importorskip("psycopg")
    calls: list[tuple[str, Any]] = []
    fetch: list[Any] = []
    embedder = FakeEmbeddingClient(dimensions=4)
    store = PgvectorKnowledgeBase(
        dsn="postgresql://x@/y", embedder=embedder, dimensions=4
    )
    monkeypatch.setattr(
        store, "_connect", lambda: _FakeConn(calls, fetch)
    )
    return store, calls, fetch


# ---------------------------------------------------------------------------
# _vector_literal — formatting
# ---------------------------------------------------------------------------

def test_vector_literal_renders_pgvector_text_form():
    assert _vector_literal([1.0, -0.5, 0.0]) == "[1.0000000,-0.5000000,0.0000000]"


def test_vector_literal_empty():
    assert _vector_literal([]) == "[]"


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------

def test_query_executes_distance_sort_with_top_k(kb):
    store, calls, fetch = kb
    fetch.extend([
        ("docs/specs/a.md", "spec", "alpha", 0.92),
        ("o/r#7", "open_issue", "beta", 0.81),
    ])
    candidates = store.query("anything", top_k=2)
    assert [c.ref for c in candidates] == ["docs/specs/a.md", "o/r#7"]
    assert [c.score for c in candidates] == [0.92, 0.81]
    assert [c.kind for c in candidates] == ["spec", "open_issue"]
    # SQL is the cosine-distance sort.
    sql, params = calls[0]
    assert "1 - (embedding <=> %s::vector)" in sql
    assert "ORDER BY embedding <=> %s::vector ASC" in sql
    assert params[2] == 2


def test_query_returns_empty_list_when_db_errors(kb, monkeypatch):
    store, _, _ = kb

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(store, "_connect", _boom)
    assert store.query("anything") == []


def test_query_returns_empty_when_embedder_errors(kb, monkeypatch):
    store, _, _ = kb

    class _BoomEmbedder:
        def embed(self, text):
            raise RuntimeError("openai down")

    monkeypatch.setattr(store, "embedder", _BoomEmbedder())
    assert store.query("text") == []


def test_query_returns_empty_when_dim_mismatch(kb, monkeypatch):
    store, _, _ = kb

    class _BadEmbedder:
        def embed(self, text):
            return [0.0] * 8  # store.dimensions == 4

    monkeypatch.setattr(store, "embedder", _BadEmbedder())
    assert store.query("text") == []


# ---------------------------------------------------------------------------
# upsert()
# ---------------------------------------------------------------------------

def test_upsert_uses_on_conflict(kb):
    store, calls, _ = kb
    store.upsert(
        kind="spec",
        ref="docs/specs/a.md",
        title="Alpha spec",
        embed_text="Alpha spec body",
    )
    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO spec_gen_embeddings" in sql
    assert "ON CONFLICT (ref) DO UPDATE" in sql
    assert params[0] == "spec"
    assert params[1] == "docs/specs/a.md"
    assert params[2] == "Alpha spec"
    # `embedding` param is a vector literal string ending in `]`.
    assert isinstance(params[4], str) and params[4].endswith("]")


def test_upsert_swallows_embed_error(kb, monkeypatch):
    store, calls, _ = kb

    class _BoomEmbedder:
        def embed(self, text):
            raise RuntimeError("openai down")

    monkeypatch.setattr(store, "embedder", _BoomEmbedder())
    store.upsert(kind="spec", ref="x", title="t", embed_text="b")
    # No SQL executed when embed fails.
    assert calls == []


def test_upsert_swallows_db_error(kb, monkeypatch):
    store, _, _ = kb

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(store, "_connect", _boom)
    # Must not raise.
    store.upsert(kind="spec", ref="x", title="t", embed_text="b")


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------

def test_delete_emits_delete_by_ref(kb):
    store, calls, _ = kb
    store.delete(ref="docs/specs/old.md")
    sql, params = calls[0]
    assert sql == "DELETE FROM spec_gen_embeddings WHERE ref = %s"
    assert params == ("docs/specs/old.md",)


# ---------------------------------------------------------------------------
# build_knowledge_base
# ---------------------------------------------------------------------------

def test_build_knowledge_base_without_dsn_returns_none():
    assert build_knowledge_base({}, FakeEmbeddingClient()) is None


def test_build_knowledge_base_without_embedder_returns_none():
    assert build_knowledge_base(
        {"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"}, None
    ) is None


def test_build_knowledge_base_with_both_returns_pgvector():
    pytest.importorskip("psycopg")
    kb = build_knowledge_base(
        {"CONTROL_PLANE_PG_DSN": "postgresql://x@/y"},
        FakeEmbeddingClient(),
    )
    assert isinstance(kb, PgvectorKnowledgeBase)


def test_duplicate_candidate_dataclass_round_trip():
    c = DuplicateCandidate(kind="spec", ref="x", title="y", score=0.5)
    assert c.kind == "spec"
    assert c.score == 0.5
