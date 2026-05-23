"""Thin HTTP client for a headless OpenCode server.

Phase A scaffold. Endpoints are verified against https://opencode.ai/docs/server
(OpenCode v1.15.7). The OpenAPI 3.1 spec served at ``<base_url>/doc`` is the
authoritative source — re-check it there if a request/response shape looks off.

Used by the Phase-A smoke test today; by ``speckit_driver.py`` and ``coder.py``
in Phases B–C.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_USERNAME = "opencode"  # OpenCode server default; override: OPENCODE_SERVER_USERNAME


class OpenCodeError(RuntimeError):
    """Raised when the OpenCode server errors or is unreachable."""


@dataclass
class Session:
    """A created OpenCode session."""

    id: str
    raw: dict[str, Any] = field(default_factory=dict)


def _text_part(text: str) -> dict[str, Any]:
    """One text message part. See the ``/doc`` OpenAPI spec for the full Part union."""
    return {"type": "text", "text": text}


def _model_ref(model: str) -> dict[str, str]:
    """Split a ``"<provider>/<model>"`` string into the OBJECT shape that
    OpenCode's ``POST /session/:id/message`` endpoint expects (rejects a bare
    string with ``"Expected object | null"``).

    NOTE: ``POST /session/:id/command`` is the inverse — it expects a STRING and
    rejects an object with ``"Expected string | null"``. The asymmetry is the
    OpenCode API's, not ours; `run_command` keeps the model string as-is.
    """
    provider, _, model_id = model.partition("/")
    return {"providerID": provider, "modelID": model_id}


class OpenCodeClient:
    """Drives a headless OpenCode server over its REST + SSE API.

    Auth is HTTP Basic — username ``opencode`` (override via ``OPENCODE_SERVER_USERNAME``),
    password from ``OPENCODE_SERVER_PASSWORD``. If no password is set the client sends
    no auth, which is fine for an unprotected local dev server.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        password: str | None = None,
        username: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("OPENCODE_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        password = (
            password if password is not None else os.environ.get("OPENCODE_SERVER_PASSWORD")
        )
        username = username or os.environ.get("OPENCODE_SERVER_USERNAME") or DEFAULT_USERNAME
        auth = (username, password) if password else None
        self._http = httpx.Client(base_url=self.base_url, auth=auth, timeout=timeout)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenCodeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"{method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise OpenCodeError(
                f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- health ------------------------------------------------------------
    def health(self) -> bool:
        """True if the server answers. The ``/event`` stream's first frame is
        ``server.connected``."""
        try:
            with self._http.stream("GET", "/event", timeout=10.0) as resp:
                if resp.status_code >= 400:
                    return False
                for line in resp.iter_lines():
                    if line:
                        return True
            return True
        except httpx.HTTPError:
            return False

    # -- sessions ----------------------------------------------------------
    def create_session(
        self, *, title: str | None = None, parent_id: str | None = None
    ) -> Session:
        """POST /session -> a new Session."""
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if parent_id:
            body["parentID"] = parent_id
        data = self._request("POST", "/session", json=body)
        return Session(id=data["id"], raw=data)

    def send_message(
        self,
        session_id: str,
        text: str,
        *,
        model: str | None = None,
        agent: str | None = None,
        system: str | None = None,
    ) -> dict[str, Any]:
        """POST /session/:id/message — blocking; returns ``{info, parts}``.

        ``model`` is ``"<provider>/<model>"`` — e.g. ``"opencode-go/deepseek-v4-pro"``
        for routine work, or ``"anthropic/claude-sonnet-4-6"`` for frontier
        escalation (plan decision 6). Omit it to use the configured default.
        """
        body: dict[str, Any] = {"parts": [_text_part(text)]}
        if model:
            body["model"] = _model_ref(model)
        if agent:
            body["agent"] = agent
        if system:
            body["system"] = system
        return self._request("POST", f"/session/{session_id}/message", json=body)

    def run_command(
        self,
        session_id: str,
        command: str,
        arguments: str = "",
        *,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """POST /session/:id/command — run a slash-command in the session.

        Used to drive the Spec-Kit commands installed as OpenCode custom commands
        (e.g. ``command="speckit.implement"``).
        """
        body: dict[str, Any] = {"command": command, "arguments": arguments}
        if model:
            # /command takes `model` as a bare "<provider>/<model>" string —
            # the inverse of /message (which wants an object). See `_model_ref`.
            body["model"] = model
        if agent:
            body["agent"] = agent
        return self._request("POST", f"/session/{session_id}/command", json=body)

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """GET /session/:id/message -> the session's messages."""
        params = {"limit": limit} if limit else None
        return self._request("GET", f"/session/{session_id}/message", params=params) or []

    def get_diff(
        self, session_id: str, *, message_id: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /session/:id/diff -> the file changes the agent produced (FileDiff[])."""
        params = {"messageID": message_id} if message_id else None
        return self._request("GET", f"/session/{session_id}/diff", params=params) or []

    # -- events ------------------------------------------------------------
    def stream_events(self) -> Iterator[dict[str, Any]]:
        """Yield decoded SSE events from GET /event (first event: ``server.connected``)."""
        with self._http.stream("GET", "/event", timeout=None) as resp:
            if resp.status_code >= 400:
                raise OpenCodeError(f"GET /event -> HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    payload = line[len("data:") :].strip()
                    if payload:
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            yield {"raw": payload}
