"""Postgres + pgvector implementation of ``KnowledgeBase`` (Tier 5).

The agent's duplicate detector talks to a ``KnowledgeBase`` Protocol
defined in ``dup_detector.py``. ``BagOfWordsKnowledgeBase`` is the
in-memory test substitute; this module is the production backing.

Wire layout:

  Agent run (GitHub Action)
      │
      ▼
  PgvectorKnowledgeBase.query("title + body", top_k=3)
      │── embedding_client.embed(text)            ← OpenAI Embeddings
      │── psycopg.connect(CONTROL_PLANE_PG_DSN)
      ▼
  SELECT ref, kind, title, 1 - (embedding <=> $1::vector) AS score
      FROM spec_gen_embeddings
      ORDER BY embedding <=> $1::vector ASC
      LIMIT $2

(``<=>`` is pgvector's cosine-distance operator; ``1 - distance`` gives the
familiar cosine *similarity* in [0, 1] for the ``DuplicateCandidate.score``
field. The threshold check is ≥ 0.85 in ``dup_detector.DUPLICATE_THRESHOLD``.)

Like ``PostgresRunStore``, this module **swallows DB errors** rather than
crashing the agent. A pgvector outage degrades dup detection to "no
candidates found", which is the right safe default.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from .dup_detector import (
    TOP_K_CANDIDATES,
    DuplicateCandidate,
    KnowledgeBase,
)
from .embedding import DEFAULT_DIMENSIONS, EmbeddingClient


def _vector_literal(vec: list[float]) -> str:
    """Render a Python ``list[float]`` as pgvector's text representation.

    psycopg can adapt lists into ``vector`` columns when the `vector`
    type is registered, but we don't want to depend on that registration
    here — using the text literal is portable across psycopg versions.
    """
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


@dataclass
class PgvectorKnowledgeBase:
    """Production `KnowledgeBase` backed by Postgres + pgvector.

    Constructor takes the DSN + an `EmbeddingClient`. The DB connection
    is created per-query (autocommit, no pool) — dup-detect is at most
    one call per agent run, so a pool would be over-engineering.
    """

    dsn: str
    embedder: EmbeddingClient
    dimensions: int = DEFAULT_DIMENSIONS
    distance_threshold: float | None = None  # surfaced for ops tuning

    def _connect(self) -> Any:
        # Imported lazily — `psycopg` is optional. The composition root
        # falls back to a no-op KB when it's missing.
        import psycopg

        return psycopg.connect(self.dsn, autocommit=True)

    def query(
        self, text: str, *, top_k: int = TOP_K_CANDIDATES
    ) -> list[DuplicateCandidate]:
        """Embed `text` and fetch the top-``top_k`` rows by cosine distance.

        Returns ``[]`` on any failure — the orchestrator treats an empty
        result as "no dup candidates", which is the safest default for a
        post-Tier-5 transient outage.
        """
        try:
            vec = self.embedder.embed(text)
        except Exception as exc:  # noqa: BLE001
            _warn(f"PgvectorKnowledgeBase.embed failed: {exc}")
            return []
        if len(vec) != self.dimensions:
            _warn(
                f"PgvectorKnowledgeBase: vector dim mismatch "
                f"({len(vec)} != {self.dimensions})"
            )
            return []

        sql = (
            "SELECT ref, kind, title, "
            "  1 - (embedding <=> %s::vector) AS score "
            "FROM spec_gen_embeddings "
            "ORDER BY embedding <=> %s::vector ASC "
            "LIMIT %s"
        )
        literal = _vector_literal(vec)
        params: tuple[Any, ...] = (literal, literal, int(top_k))
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
        except Exception as exc:  # noqa: BLE001
            _warn(f"PgvectorKnowledgeBase.query failed: {exc}")
            return []

        candidates: list[DuplicateCandidate] = []
        for row in rows:
            ref = str(row[0] or "")
            kind = str(row[1] or "")
            title = str(row[2] or "")
            score = float(row[3] or 0.0)
            candidates.append(
                DuplicateCandidate(
                    kind=kind, ref=ref, title=title, score=score
                )
            )
        return candidates

    def upsert(
        self,
        *,
        kind: str,
        ref: str,
        title: str,
        embed_text: str,
    ) -> None:
        """Embed ``embed_text`` and UPSERT one row by ``ref``.

        Used by the ingest cron (`spec_generator.ingest`). Idempotent on
        the ``UNIQUE (ref)`` index; re-running on the same ref refreshes
        the embedding (covers a spec body being edited post-merge).
        """
        try:
            vec = self.embedder.embed(embed_text or title)
        except Exception as exc:  # noqa: BLE001
            _warn(f"PgvectorKnowledgeBase.upsert.embed({ref}) failed: {exc}")
            return
        if len(vec) != self.dimensions:
            _warn(
                f"PgvectorKnowledgeBase.upsert({ref}): dim mismatch "
                f"({len(vec)} != {self.dimensions})"
            )
            return

        sql = (
            "INSERT INTO spec_gen_embeddings "
            "  (kind, ref, title, embed_text, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) "
            "ON CONFLICT (ref) DO UPDATE SET "
            "  kind = EXCLUDED.kind, "
            "  title = EXCLUDED.title, "
            "  embed_text = EXCLUDED.embed_text, "
            "  embedding = EXCLUDED.embedding, "
            "  refreshed_at = NOW()"
        )
        params = (kind, ref, title, embed_text, _vector_literal(vec))
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            _warn(f"PgvectorKnowledgeBase.upsert({ref}) failed: {exc}")

    def delete(self, *, ref: str) -> None:
        """Remove one row by ``ref`` — used when an issue closes or a
        spec is rejected (so it stops showing up as a possible dup)."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM spec_gen_embeddings WHERE ref = %s",
                    (ref,),
                )
        except Exception as exc:  # noqa: BLE001
            _warn(f"PgvectorKnowledgeBase.delete({ref}) failed: {exc}")


def _warn(msg: str) -> None:
    """Match `run_store._log_warning` so the workflow logs the same shape."""
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def build_knowledge_base(
    env: dict[str, str], embedder: EmbeddingClient | None
) -> KnowledgeBase | None:
    """Composition-root helper.

    Returns a ``PgvectorKnowledgeBase`` when both ``CONTROL_PLANE_PG_DSN``
    is set AND ``embedder`` is not ``None``; ``None`` otherwise (the
    orchestrator will skip dup detection in that case).
    """
    dsn = (env.get("CONTROL_PLANE_PG_DSN") or "").strip()
    if not dsn or embedder is None:
        return None
    try:
        import psycopg  # noqa: F401
    except ImportError:
        _warn(
            "CONTROL_PLANE_PG_DSN set but psycopg is not installed; "
            "dup detection skipped"
        )
        return None
    return PgvectorKnowledgeBase(dsn=dsn, embedder=embedder)


__all__ = [
    "PgvectorKnowledgeBase",
    "build_knowledge_base",
]
