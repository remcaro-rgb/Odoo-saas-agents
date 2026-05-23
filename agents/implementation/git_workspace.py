"""A git-backed Workspace.

Implements the `Workspace` protocol over a real git working tree via subprocess.
The git operations (checkout / read / write / commit) are integration-tested
against a throwaway temp repo.

`escalate()` records the escalation; wiring it to a real GitHub label add (via
`gh`) needs live auth and is left as Phase-D integration — see the TODO below.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .pushback import apply_session_diff as _apply_session_diff_to_root
from .workspace import Escalation


class GitWorkspace:
    """A `Workspace` backed by a real git working tree at ``root``."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.escalations: list[Escalation] = []

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def checkout(self, branch: str) -> None:
        self._git("checkout", "-B", branch)

    def read(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def exists(self, path: str) -> bool:
        return (self.root / path).is_file()

    def list_files(self, prefix: str = "") -> list[str]:
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            if ".git" in dirnames:
                dirnames.remove(".git")  # never descend into .git
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                rel = rel.replace(os.sep, "/")
                if rel.startswith(prefix):
                    found.append(rel)
        return sorted(found)

    def commit(self, paths: Iterable[str], message: str) -> str:
        self._git("add", *paths)
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    def escalate(self, reason: str, details: str = "") -> None:
        # TODO(phase-d): also add the `reason` label to the PR via `gh` — needs
        # live GitHub auth, so it is integration-wired in Phase D, not here.
        self.escalations.append(Escalation(reason, details))

    def apply_session_diff(self, diffs: list[dict[str, Any]]) -> int:
        """Apply OpenCode's session diff to the worktree via `git apply`.

        Delegates to `pushback.apply_session_diff`, which already handles the
        patch-concatenation, the protected-path defence-in-depth filter, and
        the actual `git apply` invocation. Returns the number of entries the
        underlying call actually applied (0 when the diff was empty or every
        entry was either patch-less or targeted a guardrail path).
        """
        return _apply_session_diff_to_root(str(self.root), diffs)
