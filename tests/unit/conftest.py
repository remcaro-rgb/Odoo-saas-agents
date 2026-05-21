"""Shared fakes for the Phase B / C unit tests.

Everything here runs fully offline — no live OpenCode service, no secrets.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.implementation.opencode_client import Session


class FakeOpenCodeClient:
    """In-memory stand-in for OpenCodeClient — records calls, returns canned data.

    Satisfies the surface SpecKitDriver / coder need (duck-typed).
    """

    def __init__(self) -> None:
        self.created_sessions: list[str] = []
        self.commands: list[dict[str, Any]] = []  # recorded run_command calls
        self.messages: list[tuple[str, str]] = []       # (session_id, text)
        self._command_results: dict[str, dict[str, Any]] = {}
        self._diff: list[dict[str, Any]] = []
        self._counter = 0

    # -- test controls --------------------------------------------------
    def set_command_result(self, command: str, result: dict[str, Any]) -> None:
        self._command_results[command] = result

    def set_diff(self, diff: list[dict[str, Any]]) -> None:
        self._diff = diff

    # -- OpenCodeClient surface ----------------------------------------
    def create_session(
        self, *, title: str | None = None, parent_id: str | None = None
    ) -> Session:
        self._counter += 1
        sid = f"sess-{self._counter}"
        self.created_sessions.append(sid)
        return Session(id=sid, raw={"id": sid, "title": title})

    def run_command(
        self,
        session_id: str,
        command: str,
        arguments: str = "",
        *,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        self.commands.append(
            {
                "session_id": session_id,
                "command": command,
                "arguments": arguments,
                "model": model,
                "agent": agent,
            }
        )
        return self._command_results.get(command, {"info": {}, "parts": []})

    def send_message(
        self,
        session_id: str,
        text: str,
        *,
        model: str | None = None,
        agent: str | None = None,
        system: str | None = None,
    ) -> dict[str, Any]:
        self.messages.append((session_id, text))
        return {"info": {}, "parts": []}

    def get_diff(
        self, session_id: str, *, message_id: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self._diff)

    def list_messages(
        self, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return []


@pytest.fixture()
def fake_client() -> FakeOpenCodeClient:
    return FakeOpenCodeClient()
