"""Unit tests for ingest.py — file-walk + gh-issue parse + upsert/prune."""

from __future__ import annotations

import json
import subprocess

from agents.spec_generator import ingest as ingest_module
from agents.spec_generator.embedding import FakeEmbeddingClient
from agents.spec_generator.ingest import (
    EMBED_BODY_CHARS,
    IngestStats,
    _embed_text_for,
    _truncate,
    iter_open_issues,
    iter_specs,
    run_ingest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_truncate_short():
    assert _truncate("hello") == "hello"


def test_truncate_long():
    long = "x" * (EMBED_BODY_CHARS + 50)
    out = _truncate(long)
    assert len(out) <= EMBED_BODY_CHARS + 4  # " ..." suffix
    assert out.endswith("...")


def test_embed_text_combines_title_and_body():
    out = _embed_text_for("My title", "My body content")
    assert "My title" in out
    assert "My body content" in out


# ---------------------------------------------------------------------------
# iter_specs — walks the docs dir
# ---------------------------------------------------------------------------

def test_iter_specs_yields_design_and_fix_files(tmp_path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-01-01-foo-design.md").write_text("# Foo title\nbody\n")
    (specs / "2026-01-02-bar-fix.md").write_text("# Bar title\nbody\n")
    (specs / "_TEMPLATE-design.md").write_text("# template")
    (specs / "random.txt").write_text("not markdown")

    rows = sorted(iter_specs(tmp_path), key=lambda r: r[0])
    refs = [r[0] for r in rows]
    titles = [r[1] for r in rows]
    assert refs == [
        "docs/superpowers/specs/2026-01-01-foo-design.md",
        "docs/superpowers/specs/2026-01-02-bar-fix.md",
    ]
    assert titles == ["Foo title", "Bar title"]


def test_iter_specs_falls_back_to_filename_when_no_heading(tmp_path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "no-heading-design.md").write_text("body only, no `#` heading\n")
    rows = list(iter_specs(tmp_path))
    assert rows[0][1] == "no-heading-design"


def test_iter_specs_skips_missing_directory(tmp_path):
    assert list(iter_specs(tmp_path / "nothing")) == []


def test_iter_specs_skips_templates(tmp_path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "_TEMPLATE-design.md").write_text("# template")
    (specs / "_TEMPLATE-fix.md").write_text("# template")
    assert list(iter_specs(tmp_path)) == []


# ---------------------------------------------------------------------------
# iter_open_issues — gh CLI mocked
# ---------------------------------------------------------------------------

def test_iter_open_issues_parses_gh_output(monkeypatch):
    payload = json.dumps([
        {"number": 7, "title": "First", "body": "body of 7"},
        {"number": 12, "title": "Second", "body": ""},
    ])

    def _fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(ingest_module.subprocess, "run", _fake_run)
    rows = list(iter_open_issues("o/r"))
    assert [r[0] for r in rows] == ["o/r#7", "o/r#12"]
    assert [r[1] for r in rows] == ["First", "Second"]
    assert rows[1][2] == ""


def test_iter_open_issues_gh_failure_returns_empty(monkeypatch):
    def _boom(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")

    monkeypatch.setattr(ingest_module.subprocess, "run", _boom)
    assert list(iter_open_issues("o/r")) == []


def test_iter_open_issues_invalid_json_returns_empty(monkeypatch):
    def _bad_json(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(ingest_module.subprocess, "run", _bad_json)
    assert list(iter_open_issues("o/r")) == []


# ---------------------------------------------------------------------------
# run_ingest — end-to-end against a mocked KB
# ---------------------------------------------------------------------------

class _FakeKB:
    """Captures upsert/delete calls; provides the `_connect` reach used by prune."""

    def __init__(self, *, existing_specs: list[str] | None = None,
                 existing_issues: list[str] | None = None) -> None:
        self.upserts: list[tuple[str, str, str]] = []
        self.deletes: list[str] = []
        self._existing = {
            "spec": list(existing_specs or []),
            "open_issue": list(existing_issues or []),
        }

    def upsert(self, *, kind, ref, title, embed_text):
        self.upserts.append((kind, ref, title))

    def delete(self, *, ref):
        self.deletes.append(ref)

    # `_existing_refs` calls `_connect()` then SELECTs by kind. We patch it
    # below to avoid going through a real Postgres mock here — that's
    # covered in test_spec_gen_pgvector_kb.


def test_run_ingest_indexes_and_prunes(tmp_path, monkeypatch):
    # Disk: two specs to index.
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-01-01-foo-design.md").write_text("# Foo\nbody\n")
    (specs / "2026-01-02-bar-fix.md").write_text("# Bar\nbody\n")

    # gh CLI: two open issues.
    monkeypatch.setattr(
        ingest_module.subprocess, "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps([
                {"number": 9, "title": "An issue", "body": ""},
            ]),
            stderr="",
        ),
    )

    # KB: pretend an OLD spec + an OLD issue exist that no longer match
    # our walk — they should get pruned.
    kb = _FakeKB(
        existing_specs=[
            "docs/superpowers/specs/2026-01-01-foo-design.md",
            "docs/superpowers/specs/STALE-design.md",
        ],
        existing_issues=["o/r#9", "o/r#99-stale"],
    )
    # Patch _existing_refs to surface the kb's `_existing` lists.
    monkeypatch.setattr(
        ingest_module, "_existing_refs",
        lambda kb, kind: set(kb._existing.get(kind, [])),
    )

    stats = run_ingest(kb=kb, workspace_root=tmp_path, repo="o/r")
    assert isinstance(stats, IngestStats)
    assert stats.specs_indexed == 2
    assert stats.issues_indexed == 1
    assert stats.specs_pruned == 1
    assert stats.issues_pruned == 1
    # The stale ones were deleted.
    assert "docs/superpowers/specs/STALE-design.md" in kb.deletes
    assert "o/r#99-stale" in kb.deletes


def test_run_ingest_empty_disk_empty_issues_returns_zero_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ingest_module.subprocess, "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )
    monkeypatch.setattr(
        ingest_module, "_existing_refs", lambda kb, kind: set(),
    )
    stats = run_ingest(kb=_FakeKB(), workspace_root=tmp_path, repo="o/r")
    assert stats.specs_indexed == 0
    assert stats.issues_indexed == 0
    assert stats.specs_pruned == 0
    assert stats.issues_pruned == 0


# ---------------------------------------------------------------------------
# Smoke: the module imports cleanly without OpenAI / psycopg / gh.
# ---------------------------------------------------------------------------

def test_ingest_module_imports_clean():
    # `FakeEmbeddingClient` is enough to exercise the surface without
    # any network or DB.
    assert FakeEmbeddingClient().embed("anything")
