"""Embedding client for Tier 5 duplicate detection.

The Spec Generator embeds intake bodies + indexed docs via the OpenAI
Embeddings API (text-embedding-3-small, 1536 dimensions). Pure stdlib —
no `openai` package — so the agent's pip deps stay narrow.

Two implementations:

- ``OpenAIEmbeddingClient`` — production. POSTs to ``api.openai.com/v1/embeddings``.
  Wraps ``urllib.request`` (the same pattern ``pushback.py`` uses for the
  GitHub Contents API). Includes a small retry on transient 429/5xx.
- ``FakeEmbeddingClient`` — unit-test fixture. Returns a deterministic
  vector derived from the input text; useful for orchestrator wiring tests
  without going out to the network.

Composition root picks one via ``build_embedding_client(env)`` — falls
back to ``None`` when ``OPENAI_API_KEY`` is unset, which propagates to
``build_knowledge_base`` returning ``None`` so the orchestrator skips dup
detection.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Default model + dimension. text-embedding-3-small is $0.02/M tokens and
#: matches the migration SQL's ``VECTOR(1536)`` column. Bump together.
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


@runtime_checkable
class EmbeddingClient(Protocol):
    """The minimal surface ``PgvectorKnowledgeBase`` uses."""

    def embed(self, text: str) -> list[float]: ...


class _EmbeddingError(RuntimeError):
    """Raised on unrecoverable embedding-API failures."""


@dataclass
class OpenAIEmbeddingClient:
    """OpenAI-backed embedding client.

    ``timeout_seconds`` defaults to 30s — text-embedding-3-small is fast
    (single-digit-100ms per call) so 30s is generous headroom for cold
    paths / DNS hiccups. ``max_retries`` covers transient 429 / 5xx; the
    backoff is fixed at 1s, 2s, 4s.

    ``model`` and ``dimensions`` are surfaced so a caller can switch to
    ``text-embedding-3-large`` (3072 dims) for richer dup detection if
    the false-positive rate ever proves too high — but then the
    migration's ``VECTOR(1536)`` needs a corresponding bump.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    dimensions: int = DEFAULT_DIMENSIONS
    timeout_seconds: float = 30.0
    max_retries: int = 3

    def embed(self, text: str) -> list[float]:
        """Return a 1536-d vector for ``text``.

        OpenAI clamps the input to 8192 tokens — we don't pre-clamp here
        and let the API surface the error. Empty / whitespace inputs are
        returned as a zero vector so callers can keep their indexing
        code clean.
        """
        if not (text or "").strip():
            return [0.0] * self.dimensions

        payload = json.dumps({
            "model": self.model,
            "input": text,
            "dimensions": self.dimensions,
        }).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    OPENAI_EMBEDDINGS_URL,
                    data=payload,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                        "User-Agent": "spec-generator-agent",
                    },
                )
                with urllib.request.urlopen(
                    req, timeout=self.timeout_seconds
                ) as resp:
                    body = json.loads(resp.read())
                # Shape: {data: [{embedding: [...], index: 0, ...}], ...}
                data = body.get("data") or []
                if not data:
                    raise _EmbeddingError(
                        f"OpenAI returned no embeddings: {body!r}"
                    )
                vec = data[0].get("embedding") or []
                if len(vec) != self.dimensions:
                    raise _EmbeddingError(
                        f"expected {self.dimensions}-d vector, got {len(vec)}"
                    )
                return [float(x) for x in vec]
            except urllib.error.HTTPError as exc:
                last_exc = exc
                # Retry 429 (rate-limit) and 5xx; surface 4xx (other) immediately.
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt + 1 < self.max_retries:
                        time.sleep(2 ** attempt)
                        continue
                raise _EmbeddingError(
                    f"OpenAI embeddings HTTP {exc.code}: "
                    f"{exc.read().decode('utf-8', errors='replace')[:300]}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise _EmbeddingError(
                    f"OpenAI embeddings unreachable: {type(exc).__name__}: {exc}"
                ) from exc
        # Defensive — should never reach here, but keep mypy happy.
        raise _EmbeddingError(
            f"OpenAI embeddings exhausted retries: {last_exc!r}"
        )


@dataclass
class FakeEmbeddingClient:
    """Deterministic stand-in for unit tests + offline development.

    Hashes the input text into ``dimensions`` floats in [-1, 1]. Cosine
    similarity on hashed vectors is meaningless against real embeddings
    but identical inputs produce identical vectors — enough for orchestrator
    wiring tests that just need *some* vector to flow through.
    """

    dimensions: int = DEFAULT_DIMENSIONS

    def embed(self, text: str) -> list[float]:
        text = text or ""
        # Mix entropy across the requested dimensions by hashing
        # ``text || index`` for each slot. Slow (one SHA256 per dim) but
        # only used in tests — clarity beats perf here.
        out: list[float] = []
        for i in range(self.dimensions):
            digest = hashlib.sha256(f"{text}|{i}".encode()).digest()
            n = int.from_bytes(digest[:8], "big", signed=False)
            # Map [0, 2^64) -> [-1, 1) deterministically.
            out.append((n / (2 ** 64)) * 2.0 - 1.0)
        return out


def build_embedding_client(env: dict[str, str]) -> EmbeddingClient | None:
    """Composition-root helper. Reads ``OPENAI_API_KEY``.

    Returns ``None`` when unset — the orchestrator skips dup detection
    rather than crashing.
    """
    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    return OpenAIEmbeddingClient(api_key=api_key)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Used for test assertions; the
    Postgres side uses the native `<=>` distance operator."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_MODEL",
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "OpenAIEmbeddingClient",
    "build_embedding_client",
    "cosine_similarity",
]
