"""Persistent map: PR number -> OpenCode session id (Tier 2 stub, Tier 3 SQL).

A spec PR's OpenCode session is created in Tier 2's draft step and re-entered
on every reporter comment. The session id must survive between GitHub Actions
runs (the iterate workflow is ephemeral). Tier 3 swaps the in-memory map for
a row in `spec_generator_runs`; Tier 2 ships the in-memory + JSON-file fakes
so the orchestrator can run end-to-end against fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    """Persistent map of PR -> OpenCode session id."""

    def get(self, pr: int) -> str | None: ...
    def set(self, pr: int, session_id: str) -> None: ...
    def delete(self, pr: int) -> None: ...


class InMemorySessionStore:
    """Test / fixture stand-in. Loses state at process exit."""

    def __init__(self, seed: dict[int, str] | None = None) -> None:
        self._map: dict[int, str] = dict(seed or {})

    def get(self, pr: int) -> str | None:
        return self._map.get(int(pr))

    def set(self, pr: int, session_id: str) -> None:
        self._map[int(pr)] = session_id

    def delete(self, pr: int) -> None:
        self._map.pop(int(pr), None)


class JsonFileSessionStore:
    """Tier-2-stable persistence: a JSON file on disk.

    Survives across GitHub Actions runs when the file is checked into the
    repo (acceptable for low-volume Tier 2). Tier 3's `spec_generator_runs`
    Postgres table replaces this without changing the orchestrator API.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in (data or {}).items()}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )

    def get(self, pr: int) -> str | None:
        return self._read().get(str(int(pr)))

    def set(self, pr: int, session_id: str) -> None:
        data = self._read()
        data[str(int(pr))] = session_id
        self._write(data)

    def delete(self, pr: int) -> None:
        data = self._read()
        data.pop(str(int(pr)), None)
        self._write(data)
