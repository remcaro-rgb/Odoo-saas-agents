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

#: Default dimension. Matches the migration SQL's ``VECTOR(384)`` column
#: and the self-hosted embeddings shim's BAAI/bge-small-en-v1.5 model.
#: A future swap to a larger model (768d / 1024d / 1536d) needs:
#:   1. Update the matching ``VECTOR(<dim>)`` column in
#:      migrations/2026-05-24-spec-gen-embeddings.sql (drop + re-create).
#:   2. Update ``DIMENSIONS`` in embeddings-shim/embeddings_shim/runner.py.
#:   3. Update this constant.
DEFAULT_DIMENSIONS = 384

#: Default OpenAI model (only consulted by ``OpenAIEmbeddingClient`` —
#: irrelevant when the agent uses the self-hosted shim, which is the
#: production path on this deployment).
DEFAULT_MODEL = "text-embedding-3-small"

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
class LocalEmbeddingClient:
    """Self-hosted embeddings shim client (default production path).

    POSTs to the ``embeddings-shim`` Fly service which wraps a
    sentence-transformers model (BAAI/bge-small-en-v1.5 by default,
    384-d output). Same wire shape as ``OpenAIEmbeddingClient`` — both
    satisfy the ``EmbeddingClient`` Protocol — but talks to a service
    we own + run on Fly, so dup detection has no external API
    dependency.

    See ``embeddings-shim/README.md`` for the service contract.
    """

    base_url: str
    token: str
    dimensions: int = DEFAULT_DIMENSIONS
    timeout_seconds: float = 30.0
    max_retries: int = 3

    def embed(self, text: str) -> list[float]:
        """Return a ``dimensions``-d vector for ``text``.

        Empty / whitespace -> zero vector (no HTTP call), matches
        ``OpenAIEmbeddingClient.embed`` so the agent's downstream code
        stays provider-agnostic. The shim itself ALSO short-circuits
        on empty input as defence-in-depth.
        """
        if not (text or "").strip():
            return [0.0] * self.dimensions

        payload = json.dumps({"input": text}).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(
                    url=f"{self.base_url.rstrip('/')}/embed",
                    data=payload,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.token}",
                        "User-Agent": "spec-generator-agent",
                    },
                )
                with urllib.request.urlopen(
                    req, timeout=self.timeout_seconds
                ) as resp:
                    body = json.loads(resp.read())
                vec = body.get("embedding") or []
                if len(vec) != self.dimensions:
                    raise _EmbeddingError(
                        f"shim returned {len(vec)}-d vector; expected "
                        f"{self.dimensions} (check shim model + agent dim "
                        f"match — see embeddings-shim/README.md §Model swap)"
                    )
                return [float(x) for x in vec]
            except urllib.error.HTTPError as exc:
                last_exc = exc
                # Retry 429 + 5xx; surface other 4xx (mostly 401 = bad token).
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt + 1 < self.max_retries:
                        time.sleep(2 ** attempt)
                        continue
                raise _EmbeddingError(
                    f"embeddings shim HTTP {exc.code}: "
                    f"{exc.read().decode('utf-8', errors='replace')[:300]}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise _EmbeddingError(
                    f"embeddings shim unreachable: {type(exc).__name__}: {exc}"
                ) from exc
        raise _EmbeddingError(
            f"embeddings shim exhausted retries: {last_exc!r}"
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
    """Composition-root helper. Picks a provider in this order:

    1. **Self-hosted shim** when both ``EMBEDDINGS_SHIM_URL`` and
       ``EMBEDDINGS_SHIM_TOKEN`` are set (production default — see
       ``embeddings-shim/`` in this repo).
    2. **OpenAI** when ``OPENAI_API_KEY`` is set (paid fallback).
    3. ``None`` otherwise — the orchestrator silently skips dup
       detection rather than crashing.
    """
    shim_url = (env.get("EMBEDDINGS_SHIM_URL") or "").strip()
    shim_token = (env.get("EMBEDDINGS_SHIM_TOKEN") or "").strip()
    if shim_url and shim_token:
        return LocalEmbeddingClient(base_url=shim_url, token=shim_token)

    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    if api_key:
        return OpenAIEmbeddingClient(api_key=api_key)

    return None


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
    "LocalEmbeddingClient",
    "OpenAIEmbeddingClient",
    "build_embedding_client",
    "cosine_similarity",
]
