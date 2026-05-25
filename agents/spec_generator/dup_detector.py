"""Duplicate detection via embedding search (Tier 5).

When a new issue arrives, the duplicate detector embeds the issue text and
queries the pgvector index of (a) open spec PRs and (b) currently-open
issues. Cosine similarity >= 0.85 to the top hit flags the new issue as a
possible duplicate; the bot prefixes the spec PR title with `[possible-dup]`
and links the candidate so the reporter can decide.

The implementation is intentionally Protocol-based — the production wire is
an HTTP call to the Vercel-side embeddings + Postgres+pgvector backend, but
the unit tests run against an in-memory `KnowledgeBase` that scores literal
overlap. Real-world tuning lands at Tier 5 exit per the plan §3.

Q4 from the plan: open issues + closed-as-merged specs only. Closed-rejected
specs are deliberately NOT in the index — they'd resurrect stale "we
considered this and said no" cases.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .intake import Intake

# Cosine-similarity threshold to call something a duplicate.
#
# The plan §3 Tier 5 originally specified 0.85, which was calibrated for
# OpenAI text-embedding-3-* models. We migrated to a self-hosted
# `BAAI/bge-small-en-v1.5` shim on 2026-05-24 (see embeddings-shim/),
# which has a different similarity distribution: topically-related pairs
# score 0.5-0.8, with 0.85+ reserved for essentially-identical text.
#
# Calibration data — first canary on 2026-05-25:
#   query #54 ("Add bulk-archive action to /partner list")
#     top hit #47 ("Add CSV export to sale orders list") → 0.6653
#   query #67 ("Add CSV export from /partner list")
#     top hit #47 → ~0.66 (inferred)
#
# 0.65 catches the obvious near-dups; tune up to 0.70-0.75 if false-
# positives become noisy. The plan's "tune from the first 100 runs"
# note (§3 Tier 5) anticipated this exact step.
DUPLICATE_THRESHOLD = 0.65

# Cap on how many candidate dups we surface (more than this is noise).
TOP_K_CANDIDATES = 3


@dataclass(frozen=True)
class DuplicateCandidate:
    """A possible-dup the index returned for a query."""

    kind: str        # "spec" | "open_issue"
    ref: str         # spec path or issue URL
    title: str
    score: float


@dataclass(frozen=True)
class DuplicateResult:
    """The duplicate-detector verdict.

    `is_duplicate` is `True` when at least one candidate scores
    `>= DUPLICATE_THRESHOLD`. The orchestrator can still surface
    sub-threshold candidates as suggestions; only `is_duplicate` triggers
    the title prefix.
    """

    is_duplicate: bool
    candidates: tuple[DuplicateCandidate, ...]

    @property
    def top(self) -> DuplicateCandidate | None:
        return self.candidates[0] if self.candidates else None


@runtime_checkable
class KnowledgeBase(Protocol):
    """The embedding-index surface the detector uses."""

    def query(
        self, text: str, *, top_k: int = TOP_K_CANDIDATES
    ) -> list[DuplicateCandidate]: ...


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


def _bow(text: str) -> Counter[str]:
    return Counter(token.lower() for token in _WORD.findall(text or ""))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity between two bag-of-words Counters.

    Tiny implementation so the unit-test substitute can produce
    deterministic-but-realistic scores without dragging numpy into the
    test path.
    """
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class BagOfWordsKnowledgeBase:
    """In-memory `KnowledgeBase` for unit tests (and bootstrap).

    Production swaps this for the Vercel/pgvector wire — same shape.
    """

    def __init__(self) -> None:
        self._items: list[tuple[DuplicateCandidate, Counter[str]]] = []

    def add(self, candidate: DuplicateCandidate, text: str) -> None:
        self._items.append((candidate, _bow(text)))

    def query(
        self, text: str, *, top_k: int = TOP_K_CANDIDATES
    ) -> list[DuplicateCandidate]:
        query_bow = _bow(text)
        scored = [
            DuplicateCandidate(
                kind=c.kind, ref=c.ref, title=c.title,
                score=_cosine(query_bow, doc_bow),
            )
            for c, doc_bow in self._items
        ]
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]


@dataclass
class DuplicateDetector:
    """Glue: embed the intake, ask the KB, decide."""

    kb: KnowledgeBase

    def detect(self, intake: Intake) -> DuplicateResult:
        query = f"{intake.title}\n{intake.body}".strip()
        candidates = self.kb.query(query, top_k=TOP_K_CANDIDATES)
        candidates = [c for c in candidates if c.score > 0]
        is_dup = bool(candidates) and candidates[0].score >= DUPLICATE_THRESHOLD
        return DuplicateResult(is_duplicate=is_dup, candidates=tuple(candidates))


def render_duplicate_callout(result: DuplicateResult) -> str:
    """Markdown for the bot's "possible dup" suggestion."""
    if not result.candidates:
        return ""
    lines = ["**Possible duplicates:**"]
    for c in result.candidates:
        kind_label = "spec" if c.kind == "spec" else "open issue"
        lines.append(
            f"- {kind_label}: [{c.title}]({c.ref}) "
            f"_(similarity {c.score:.2f})_"
        )
    return "\n".join(lines)


def title_prefix_for(result: DuplicateResult) -> str | None:
    """`[possible-dup]` when above threshold, else `None`."""
    return "[possible-dup]" if result.is_duplicate else None
