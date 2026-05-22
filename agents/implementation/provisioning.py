"""Provision an OpenCode session's workspace from a git branch (Phase D).

OpenCode operates on the agent's per-spec git checkout. `provision_workspace`
drives a session to make its workspace a fresh checkout of a repo branch — the
clone authenticates over HTTPS with the `GITHUB_TOKEN` Fly secret already in the
container, referenced by env var so no token is ever embedded here.
"""

from __future__ import annotations

from typing import Any

# The container reads its GitHub token from this env var (a Fly secret).
_TOKEN_ENV = "GITHUB_TOKEN"


def _provision_script(repo: str, branch: str, workdir: str) -> str:
    """The bash that resets `workdir` to a fresh, shallow checkout of repo@branch.

    `git init` is idempotent, so re-provisioning the same workspace is safe; the
    closing `git clean` drops any files left from a previous spec.
    """
    url = f"https://x-access-token:${{{_TOKEN_ENV}}}@github.com/{repo}.git"
    return (
        f"cd {workdir} && "
        "git init -q && "
        "git remote remove origin 2>/dev/null; "
        f"git remote add origin {url} && "
        f"git fetch -q --depth 1 origin {branch} && "
        f"git checkout -q -f -B {branch} FETCH_HEAD && "
        "git clean -qfdx"
    )


def provision_workspace(
    client: Any,
    session_id: str,
    repo: str,
    branch: str,
    *,
    workdir: str = "/workspace",
) -> dict[str, Any]:
    """Make the OpenCode session's `workdir` a fresh checkout of `repo` @ `branch`.

    `repo` is a `owner/name` slug. The clone authenticates with the container's
    `GITHUB_TOKEN`; the token is referenced by env var and never embedded here.
    """
    script = _provision_script(repo, branch, workdir)
    return client.send_message(
        session_id,
        f"Run exactly this bash command and report its output verbatim:\n\n{script}",
    )
