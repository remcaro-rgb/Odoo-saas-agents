"""FastAPI entry point for the embeddings shim.

Three endpoints:

- ``GET  /healthz``  — Fly health check + liveness probe. Loads the model
  lazily on first call so the container can start while the model
  downloads in the background.
- ``POST /embed``    — bearer-token-protected embedding lookup. Body
  ``{"input": "..."}``, response ``{"embedding": [float, ...]}``.
- ``GET  /``         — service info (unauthenticated; safe to expose).

The actual model load + inference lives in :mod:`embeddings_shim.runner` so
the HTTP layer is just auth + validation.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .runner import DIMENSIONS, MODEL_NAME, embed_text

#: The shared bearer token the agent sends. Set on both sides as a secret
#: named ``EMBEDDINGS_SHIM_TOKEN``. Missing -> the service refuses every
#: request (fail-closed), so a stray deploy without the secret can't
#: accidentally expose the embedding pool.
EXPECTED_TOKEN = os.environ.get("EMBEDDINGS_SHIM_TOKEN")


app = FastAPI(
    title="embeddings-shim",
    description=(
        "Spec Generator's self-hosted embedding service. Wraps "
        f"`{MODEL_NAME}` ({DIMENSIONS} dimensions). See README.md."
    ),
    version="0.1.0",
)


class EmbedRequest(BaseModel):
    input: str = Field(default="", description="text to embed; empty -> zero vector")


class EmbedResponse(BaseModel):
    embedding: list[float] = Field(description=f"{DIMENSIONS}-d vector")


class HealthResponse(BaseModel):
    status: str
    model: str
    dimensions: int
    auth_configured: bool


class RootResponse(BaseModel):
    service: str
    docs: str
    health: str
    model: str
    dimensions: int


def _require_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """Bearer-token guard. Fails closed."""
    if not EXPECTED_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="shim misconfigured: EMBEDDINGS_SHIM_TOKEN unset",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Bearer token",
        )
    token = authorization[len("Bearer ") :].strip()
    if token != EXPECTED_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Public liveness probe. Does NOT trigger model load."""
    return HealthResponse(
        status="ok",
        model=MODEL_NAME,
        dimensions=DIMENSIONS,
        auth_configured=bool(EXPECTED_TOKEN),
    )


@app.get("/", response_model=RootResponse)
def root() -> RootResponse:
    """Friendly service-info root (the agentlab shim's omission of this
    caught us during smoke tests; embeddings shim gets one upfront)."""
    return RootResponse(
        service="embeddings-shim",
        docs="/docs",
        health="/healthz",
        model=MODEL_NAME,
        dimensions=DIMENSIONS,
    )


@app.post(
    "/embed",
    response_model=EmbedResponse,
    dependencies=[Depends(_require_token)],
)
def embed(payload: EmbedRequest) -> EmbedResponse:
    """Embed ``payload.input``. Empty input -> zero vector (no model call).

    The model is loaded lazily inside :func:`embed_text` so the container
    starts fast and Fly's health-check passes within the grace period.
    Subsequent requests reuse the in-process model.
    """
    vec = embed_text(payload.input)
    return EmbedResponse(embedding=vec)
