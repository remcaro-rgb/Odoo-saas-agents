"""FastAPI entry point for the agentlab Playwright shim.

Two endpoints:

- ``GET  /healthz``  — Fly health-check + liveness probe. Always returns 200.
- ``POST /repro``    — the reproduction attempt. Auth: ``Bearer <token>``.
  Body shape locked by ``agents/spec_generator/repro.py:HttpShimAgentlabClient``.

The actual Playwright orchestration lives in ``runner.py`` — this module is
just the HTTP layer + auth + request validation.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .runner import ReproRequest, ReproResponse, run_repro

#: The shared bearer token the Spec Generator sends. Set on both sides as
#: a secret (``AGENTLAB_SHIM_TOKEN``). Missing here = the service refuses
#: every request, which fails closed.
EXPECTED_TOKEN = os.environ.get("AGENTLAB_SHIM_TOKEN")

#: Default agentlab URL; can be overridden per-request via the ``base_url``
#: body field (Tier-4 leaves it global for now).
DEFAULT_AGENTLAB_BASE = os.environ.get(
    "AGENTLAB_BASE_URL", "https://odoo-saas-odoo-agentlab.fly.dev"
)


app = FastAPI(
    title="agentlab-shim",
    description=(
        "Spec Generator's HTTP front for Playwright reproductions against "
        "the agentlab Odoo tenant. See README.md for the contract."
    ),
    version="0.1.0",
)


class ReproRequestBody(BaseModel):
    """Request shape — mirrors the agent's ``HttpShimAgentlabClient`` payload."""

    issue: int
    title: str = Field(default="")
    body: str = Field(default="")
    attachments: list[str] = Field(default_factory=list)
    reporter: str = Field(default="unknown")


class HealthResponse(BaseModel):
    status: str
    auth_configured: bool


def _require_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """Bearer token guard. Fails closed on missing/wrong tokens.

    A misconfigured server (``EXPECTED_TOKEN`` unset) is treated as
    fail-closed — every request is rejected — so a stray deploy without
    the secret can't accidentally expose the Playwright pool.
    """
    if not EXPECTED_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="shim misconfigured: AGENTLAB_SHIM_TOKEN unset",
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
    """Liveness probe. Unauthenticated (Fly needs it to route traffic)."""
    return HealthResponse(
        status="ok",
        auth_configured=bool(EXPECTED_TOKEN),
    )


@app.post("/repro", response_model=ReproResponse, dependencies=[Depends(_require_token)])
def repro(payload: ReproRequestBody) -> ReproResponse:
    """Reproduce the bug described in ``payload`` against agentlab.

    Always returns 200 with a structured outcome — the client uses
    ``outcome`` to route to confirmed / needs-info / needs-fixture /
    agentlab-unavailable. Errors that leak to a 5xx are runtime bugs in
    the shim itself, not the reporter's reproduction steps.
    """
    request = ReproRequest(
        issue=payload.issue,
        title=payload.title,
        body=payload.body,
        attachments=tuple(payload.attachments),
        reporter=payload.reporter,
        base_url=DEFAULT_AGENTLAB_BASE,
    )
    return run_repro(request)
