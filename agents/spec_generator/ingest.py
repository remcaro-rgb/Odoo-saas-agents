"""Embedding-index ingest job (Tier 5).

Walks two sources, embeds each document, UPSERTs into the
``spec_gen_embeddings`` table via the `PgvectorKnowledgeBase.upsert`
surface. Driven by a daily cron workflow
(`deploy/workflows/spec-generator-embed-ingest.yml`).

Sources:

- **Merged design specs + fix-briefs** under
  ``docs/superpowers/specs/**/*.md`` on the data-plane repo. Anything
  matching ``*-design.md`` or ``*-fix.md`` and NOT ``_TEMPLATE-*.md``.
- **Open GitHub issues** on the data-plane repo. We index the title +
  first ``EMBED_BODY_CHARS`` of the body so the vector represents the
  intake's intent without bloating the embedding payload.

After indexing, the job **prunes**:

- Specs that no longer exist on disk (a rejected PR closed without merge).
- Issues that closed without being merged (``state: closed`` and no
  associated PR or merged spec).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .pgvector_kb import PgvectorKnowledgeBase

#: First N characters of an issue / spec body included in the embedded
#: text. Keeps token cost down (≈ 125 chars/token average) and avoids
#: indexing acceptance-criteria boilerplate that drowns out signal.
EMBED_BODY_CHARS = 500

SPEC_DIR = "docs/superpowers/specs"


@dataclass
class IngestStats:
    specs_indexed: int = 0
    issues_indexed: int = 0
    specs_pruned: int = 0
    issues_pruned: int = 0


def _truncate(text: str, limit: int = EMBED_BODY_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def _embed_text_for(title: str, body: str) -> str:
    """The string we actually pass to the embedding model."""
    return f"{title.strip()}\n\n{_truncate(body)}"


# ---------------------------------------------------------------------------
# Source iterators — pure-data, easy to mock in tests
# ---------------------------------------------------------------------------

def iter_specs(workspace_root: str | Path) -> Iterator[tuple[str, str, str]]:
    """Walk ``docs/superpowers/specs/**/*.md`` on disk.

    Yields ``(ref, title, body)`` for each file. ``ref`` is the
    repo-relative path; ``title`` is the first markdown heading.
    """
    root = Path(workspace_root) / SPEC_DIR
    if not root.exists():
        return
    for path in sorted(root.rglob("*.md")):
        name = path.name
        if name.startswith("_TEMPLATE"):
            continue
        if not (name.endswith("-design.md") or name.endswith("-fix.md")):
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        title = _first_heading(body) or path.stem
        rel = str(path.relative_to(workspace_root))
        yield rel, title, body


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None


def iter_open_issues(
    repo: str,
    *,
    limit: int = 200,
) -> Iterator[tuple[str, str, str]]:
    """Walk open GitHub issues via ``gh`` CLI.

    Yields ``(ref, title, body)``. ``ref`` is ``<repo>#<number>`` per the
    migration's ``UNIQUE (ref)`` index.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--state", "open",
                "--limit", str(limit),
                "--json", "number,title,body",
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        _warn(f"iter_open_issues({repo}) failed: {exc}")
        return
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        rows = []
    for row in rows:
        num = row.get("number")
        if num is None:
            continue
        yield f"{repo}#{num}", str(row.get("title") or ""), str(row.get("body") or "")


# ---------------------------------------------------------------------------
# Pruning: rows whose source has disappeared
# ---------------------------------------------------------------------------

def _existing_refs(
    kb: PgvectorKnowledgeBase, kind: str
) -> set[str]:
    """All ``ref`` values currently in the index for one ``kind``."""
    try:
        with kb._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ref FROM spec_gen_embeddings WHERE kind = %s",
                (kind,),
            )
            return {str(row[0]) for row in cur.fetchall() or []}
    except Exception as exc:  # noqa: BLE001
        _warn(f"_existing_refs({kind}) failed: {exc}")
        return set()


# ---------------------------------------------------------------------------
# Orchestrator: one call drives the whole pass
# ---------------------------------------------------------------------------

def run_ingest(
    *,
    kb: PgvectorKnowledgeBase,
    workspace_root: str | Path,
    repo: str,
) -> IngestStats:
    """Walk both sources, upsert each row, prune missing.

    Returns counts the cron workflow surfaces in its `sweep-summary`-shaped
    record so the Axiom dashboard can graph ingest health.
    """
    stats = IngestStats()

    # Specs ----------------------------------------------------------------
    seen_spec_refs: set[str] = set()
    for ref, title, body in iter_specs(workspace_root):
        kb.upsert(
            kind="spec",
            ref=ref,
            title=title,
            embed_text=_embed_text_for(title, body),
        )
        seen_spec_refs.add(ref)
        stats.specs_indexed += 1

    # Open issues ---------------------------------------------------------
    seen_issue_refs: set[str] = set()
    for ref, title, body in iter_open_issues(repo):
        kb.upsert(
            kind="open_issue",
            ref=ref,
            title=title,
            embed_text=_embed_text_for(title, body),
        )
        seen_issue_refs.add(ref)
        stats.issues_indexed += 1

    # Prune ---------------------------------------------------------------
    for stale in _existing_refs(kb, "spec") - seen_spec_refs:
        kb.delete(ref=stale)
        stats.specs_pruned += 1
    for stale in _existing_refs(kb, "open_issue") - seen_issue_refs:
        kb.delete(ref=stale)
        stats.issues_pruned += 1

    return stats


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Entry point for `python -m agents.spec_generator.ingest`
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry — the cron workflow shells out to this."""
    from agents.implementation.observability import EventLog  # noqa: I001

    from .embedding import build_embedding_client
    from .pgvector_kb import build_knowledge_base

    env = dict(os.environ)
    log = EventLog(run_id=env.get("GITHUB_RUN_ID"))
    workspace_root = env.get("WORKSPACE_ROOT") or "."
    repo = env.get("DATA_PLANE_REPO") or ""
    if not repo:
        log.emit("misconfigured", detail="DATA_PLANE_REPO is required")
        return 1

    embedder = build_embedding_client(env)
    if embedder is None:
        log.emit("misconfigured", detail="OPENAI_API_KEY is required for ingest")
        return 1
    kb = build_knowledge_base(env, embedder)
    if kb is None or not isinstance(kb, PgvectorKnowledgeBase):
        log.emit(
            "misconfigured",
            detail="CONTROL_PLANE_PG_DSN is required (or psycopg missing)",
        )
        return 1

    log.emit("ingest-start", repo=repo, workspace=workspace_root)
    stats = run_ingest(kb=kb, workspace_root=workspace_root, repo=repo)
    log.emit(
        "ingest-summary",
        specs_indexed=stats.specs_indexed,
        issues_indexed=stats.issues_indexed,
        specs_pruned=stats.specs_pruned,
        issues_pruned=stats.issues_pruned,
    )
    return 0


__all__ = [
    "EMBED_BODY_CHARS",
    "IngestStats",
    "iter_open_issues",
    "iter_specs",
    "main",
    "run_ingest",
]


if __name__ == "__main__":
    # `python -m agents.spec_generator.ingest` — the cron workflow's entry
    # point. Without this guard the module imports cleanly + exits 0 with
    # no work done (caught live on 2026-05-25 ingest run 26382803631 —
    # 460ms exit, zero stdout).
    raise SystemExit(main())
