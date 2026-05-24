"""Unit tests for InMemorySessionStore + JsonFileSessionStore."""

from __future__ import annotations

from agents.spec_generator.session_store import (
    InMemorySessionStore,
    JsonFileSessionStore,
)


def test_in_memory_round_trip():
    store = InMemorySessionStore()
    assert store.get(7) is None
    store.set(7, "sess-1")
    assert store.get(7) == "sess-1"
    store.delete(7)
    assert store.get(7) is None


def test_in_memory_seed_dict():
    store = InMemorySessionStore({1: "a", 2: "b"})
    assert store.get(1) == "a"
    assert store.get(2) == "b"


def test_json_file_store_round_trip(tmp_path):
    path = tmp_path / "sessions.json"
    store = JsonFileSessionStore(path)
    assert store.get(7) is None
    store.set(7, "sess-1")
    assert path.exists()
    # A fresh store reads from disk.
    fresh = JsonFileSessionStore(path)
    assert fresh.get(7) == "sess-1"
    fresh.delete(7)
    assert JsonFileSessionStore(path).get(7) is None


def test_json_file_store_tolerates_corrupted_file(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not json", encoding="utf-8")
    store = JsonFileSessionStore(path)
    assert store.get(7) is None
    store.set(7, "ok")
    assert JsonFileSessionStore(path).get(7) == "ok"
