"""The Workspace seam (Phase B).

The orchestrator's side effects — read/write files, checkout, commit, escalate —
go through a `Workspace`. Tests run against `InMemoryWorkspace`; it also doubles
as the shadow-mode workspace (Phase F: the agent runs but nothing is pushed).

The real git/GitHub-backed `GitWorkspace` is Phase-D infra wiring and is not part
of this offline-buildable slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Commit:
    paths: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Escalation:
    reason: str
    details: str = ""


@runtime_checkable
class Workspace(Protocol):
    """Everything the orchestrator needs from the working tree + PR."""

    def checkout(self, branch: str) -> None: ...
    def read(self, path: str) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    def exists(self, path: str) -> bool: ...
    def list_files(self, prefix: str = "") -> list[str]: ...
    def commit(self, paths: Iterable[str], message: str) -> str: ...
    def escalate(self, reason: str, details: str = "") -> None: ...


class InMemoryWorkspace:
    """An in-memory `Workspace` — for unit tests and for shadow-mode runs."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.branch: str | None = None
        self.commits: list[Commit] = []
        self.escalations: list[Escalation] = []

    def checkout(self, branch: str) -> None:
        self.branch = branch

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path: str, content: str) -> None:
        self.files[path] = content

    def exists(self, path: str) -> bool:
        return path in self.files

    def list_files(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix))

    def commit(self, paths: Iterable[str], message: str) -> str:
        self.commits.append(Commit(tuple(paths), message))
        return f"commit-{len(self.commits)}"

    def escalate(self, reason: str, details: str = "") -> None:
        self.escalations.append(Escalation(reason, details))
