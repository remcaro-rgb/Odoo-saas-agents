"""Provision an OpenCode session's workspace from a git branch (Phase D).

OpenCode operates on the agent's per-spec git checkout. `provision_workspace`
drives a session to make its workspace a fresh checkout of a repo branch — the
clone authenticates over HTTPS with the `GITHUB_TOKEN` Fly secret already in the
container, referenced by env var so no token is ever embedded here.

The checkout is *sparse*: the guardrail paths (`infra/`, `.github/`, `Dockerfile`,
`saas_tenant_gate/security/`) are excluded from the agent's worktree, so the
agent's bash tool cannot reach the repo's copy of them (task #58 — this
complements the opencode.json `edit`/`bash` deny-lists and the non-root user).
"""

from __future__ import annotations

from typing import Any

# The container reads its GitHub token from this env var (a Fly secret).
_TOKEN_ENV = "GITHUB_TOKEN"

# Guardrail paths excluded from the agent's sparse checkout — it never receives
# the repo's copy of these, so its bash tool cannot modify them. Mirrors the
# opencode.json `edit` permission deny-list.
_PROTECTED_PATHS = ("infra/", ".github/", "Dockerfile", "saas_tenant_gate/security/")


def _provision_script(
    repo: str, branch: str, workdir: str, *, shadow: bool = False
) -> str:
    """The bash that resets `workdir` to a fresh, shallow, sparse checkout of
    repo@branch.

    `git init` is idempotent, so re-provisioning the same workspace is safe; the
    closing `git clean` drops any files left from a previous spec. The checkout
    is sparse (`core.sparseCheckout`): `_PROTECTED_PATHS` are excluded, so the
    guardrail files never land in the agent's worktree.

    SHADOW mode (`shadow=True`) appends a `git remote set-url origin <unauth>`
    step that drops the token from `origin` after the initial fetch. Any
    subsequent autonomous `git push` the container attempts then fails 401 —
    that is what makes SHADOW genuinely "post / push nothing" (runbook §7).
    """
    auth_url = f"https://x-access-token:${{{_TOKEN_ENV}}}@github.com/{repo}.git"
    # Old-style sparse-checkout patterns: include everything (`/*`), then exclude
    # the guardrail paths. Written to .git/info/sparse-checkout before checkout.
    patterns = ["/*", *(f"!/{path}" for path in _PROTECTED_PATHS)]
    sparse = " ".join(f"'{pattern}'" for pattern in patterns)
    script = (
        f"cd {workdir} && "
        "git init -q && "
        "git remote remove origin 2>/dev/null; "
        f"git remote add origin {auth_url} && "
        "git config core.sparseCheckout true && "
        f"printf '%s\\n' {sparse} > .git/info/sparse-checkout && "
        f"git fetch -q --depth 1 origin {branch} && "
        f"git checkout -q -f -B {branch} FETCH_HEAD && "
        "git clean -qfdx"
    )
    if shadow:
        unauth_url = f"https://github.com/{repo}.git"
        script += f" && git remote set-url origin {unauth_url}"
    return script


def provision_workspace(
    client: Any,
    session_id: str,
    repo: str,
    branch: str,
    *,
    workdir: str = "/workspace",
    shadow: bool = False,
) -> dict[str, Any]:
    """Make the OpenCode session's `workdir` a fresh checkout of `repo` @ `branch`.

    `repo` is a `owner/name` slug. The clone authenticates with the container's
    `GITHUB_TOKEN`; the token is referenced by env var and never embedded here.
    When `shadow=True` the auth is stripped from `origin` after the fetch so the
    container cannot push (the rollout SHADOW guarantee).
    """
    script = _provision_script(repo, branch, workdir, shadow=shadow)
    return client.send_message(
        session_id,
        f"Run exactly this bash command and report its output verbatim:\n\n{script}",
    )
