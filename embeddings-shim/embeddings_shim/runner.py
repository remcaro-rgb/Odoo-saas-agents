"""Sentence-Transformers wrapper — single-model in-process inference.

The model loads lazily on first call (the Fly container starts faster
than the model can download from HuggingFace; we let /healthz pass
during the load window). Subsequent calls reuse the cached instance.

L2-normalized output so the agent's pgvector cosine-distance index
behaves as expected — bge-small-en-v1.5 returns normalized vectors by
default, but we re-normalize defensively in case the model swap
introduces one that doesn't.
"""

from __future__ import annotations

import math
from threading import Lock
from typing import Any

#: Default model. Swap by editing this line + the matching ``VECTOR(<dim>)``
#: column in ``migrations/2026-05-24-spec-gen-embeddings.sql``. See README.md.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# Lazy global cache — `_model` is `None` until the first /embed call.
_model: Any = None
_model_lock = Lock()


def _load_model() -> Any:
    """Load the sentence-transformer once, cached for the process lifetime."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:  # double-checked locking
            return _model
        # Import lazily so the test suite doesn't drag torch in for every run.
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector so cosine == dot product on the pgvector side."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def embed_text(text: str) -> list[float]:
    """Return a ``DIMENSIONS``-d L2-normalized vector for ``text``.

    Empty / whitespace input short-circuits to a zero vector — matches
    the OpenAI client's behaviour so the agent's downstream code stays
    provider-agnostic.
    """
    if not (text or "").strip():
        return [0.0] * DIMENSIONS
    model = _load_model()
    # `encode` returns a numpy array; convert to plain list[float] for the
    # JSON response.
    arr = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
    vec: list[float] = [float(x) for x in arr.tolist()]
    if len(vec) != DIMENSIONS:
        raise RuntimeError(
            f"model returned {len(vec)}-d vector; expected {DIMENSIONS}"
        )
    return _normalize(vec)


__all__ = ["DIMENSIONS", "MODEL_NAME", "embed_text"]
