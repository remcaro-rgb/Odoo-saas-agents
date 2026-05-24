"""Push the drafted spec to a new branch and open the spec PR (Tier 2).

Uses the GitHub Contents API instead of ``git commit`` + ``git push`` so
GitHub server-signs the commit with the App's verified key. Required by the
``agent-spec-branches`` ruleset on ``GoliattCo/odoo-custom``: pushes to
``refs/heads/agent/spec-*`` are rejected unless ``required_signatures`` is
satisfied, and a runner-side ``git commit`` produces an unsigned commit.

Flow:

1. Resolve the base branch's tip SHA (``GET /repos/.../branches/{base}``).
2. Look up the spec file on the agent branch if it exists
   (``GET /repos/.../contents/{path}?ref=<branch>``) — yields the blob SHA
   for an update, or 404 for a create.
3. Create or update the file in one call
   (``PUT /repos/.../contents/{path}`` with ``branch`` and optional ``sha``).
   GitHub creates the branch automatically when it doesn't exist, derives
   a single-file commit from the base branch's tip, and signs it.
4. Open the spec PR if one doesn't already exist.

Shadow-aware: SHADOW short-circuits before any HTTP call.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from agents.implementation.observability import EventLog
from agents.implementation.rollout import RolloutDecision

BOT_NAME = "spec-generator-bot[bot]"
GITHUB_API_BASE = "https://api.github.com"


def _bot_email(app_id: str | None) -> str:
    """The canonical ``<id>+spec-generator-bot[bot]@users.noreply.github.com``."""
    if not app_id:
        return "spec-generator-bot[bot]@users.noreply.github.com"
    return f"{app_id}+spec-generator-bot[bot]@users.noreply.github.com"


@dataclass
class _GhResponse:
    status: int
    body: dict[str, Any] | list[Any] | None
    raw: bytes


def _gh_request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    accept_404: bool = False,
    accept_422: bool = False,
) -> _GhResponse:
    """Authed GitHub REST call. Returns parsed JSON + status.

    ``accept_404`` makes a missing resource (typically a file that doesn't
    exist yet on the branch) a non-error response so the caller can branch
    on ``status``. ``accept_422`` makes "already exists" (the idempotent
    case for `POST /git/refs` against an existing branch) a non-error.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url=f"{GITHUB_API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "spec-generator-bot",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read()
            return _GhResponse(
                status=resp.status,
                body=json.loads(raw) if raw else None,
                raw=raw,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed: dict[str, Any] | None = None
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    parsed = loaded
            except json.JSONDecodeError:
                parsed = None
        if exc.code == 404 and accept_404:
            return _GhResponse(status=404, body=parsed, raw=raw)
        if exc.code == 422 and accept_422:
            return _GhResponse(status=422, body=parsed, raw=raw)
        body_text = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"GitHub {method} {path} -> HTTP {exc.code}: {body_text}"
        ) from exc


def push_spec(
    *,
    workspace_root: str,           # kept for parity with the impl-bot's API; unused here
    spec_path: str,
    spec_body: str,
    branch: str,
    push_url: str,                 # legacy; only used to extract the bot token
    issue: int,
    title: str,
    decision: RolloutDecision,
    log: EventLog,
    app_id: str | None = None,
    base_branch: str = "main",
    repo: str | None = None,
    token: str | None = None,
) -> int | None:
    """Create or update the spec file on ``branch`` and open the spec PR.

    Returns the PR number on success, or ``None`` for SHADOW / PR-creation
    deferral. The commit is server-signed by GitHub because the API is
    called with the spec-generator-bot App's installation token.

    Backwards-compat: callers that still pass ``push_url`` with the bot
    token embedded (the old ``git push`` shape) get the token extracted
    automatically. New callers should pass ``token`` + ``repo`` directly.
    """
    if decision is RolloutDecision.SHADOW:
        log.emit(
            "shadow-push",
            branch=branch,
            spec_path=spec_path,
            bytes=len(spec_body or ""),
            issue=issue,
        )
        return None

    token = token or _extract_token(push_url)
    if token is None:
        log.emit("push-skipped", reason="no GitHub token available")
        return None
    repo = repo or _extract_repo(push_url)
    if repo is None:
        log.emit("push-skipped", reason="cannot resolve owner/repo from push URL")
        return None

    # 1. Resolve the base branch's tip SHA — only needed to verify the base
    # exists before we try to create the agent branch off it.
    base_resp = _gh_request(
        "GET", f"/repos/{repo}/branches/{base_branch}", token=token
    )
    base_body = base_resp.body if isinstance(base_resp.body, dict) else {}
    base_sha = (base_body.get("commit") or {}).get("sha")
    if not base_sha:
        raise RuntimeError(
            f"cannot resolve base branch {base_branch} on {repo}"
        )

    # 1b. Ensure the agent branch exists. `PUT /contents` does NOT auto-create
    # the branch (despite what the docs suggest); a missing branch returns
    # 404. We always try `POST /git/refs` — a 422 "Reference already exists"
    # is the idempotent success case and is swallowed via `accept_422`.
    create_ref = _gh_request(
        "POST",
        f"/repos/{repo}/git/refs",
        token=token,
        body={"ref": f"refs/heads/{branch}", "sha": base_sha},
        accept_404=False,
        accept_422=True,
    )
    if create_ref.status == 201:
        log.emit("branch-created", branch=branch, base_sha=base_sha[:8])
    elif create_ref.status == 422:
        # 422 = "Reference already exists" most of the time, but can also
        # be "Invalid SHA" / "Reference does not match expected pattern".
        # Distinguish via the body's `message` field — only treat the
        # already-exists case as success; anything else must raise.
        msg = ""
        if isinstance(create_ref.body, dict):
            msg = str(create_ref.body.get("message") or "")
        if "already exists" in msg.lower():
            log.emit("branch-exists", branch=branch)
        else:
            raise RuntimeError(
                f"POST /repos/{repo}/git/refs -> 422 (not already-exists): {msg}"
            )

    # 2. Check whether the file already exists on the agent branch (returns
    # the blob SHA needed for an UPDATE).
    existing = _gh_request(
        "GET",
        f"/repos/{repo}/contents/{spec_path}?ref={branch}",
        token=token,
        accept_404=True,
    )
    existing_sha = None
    if existing.status == 200 and isinstance(existing.body, dict):
        existing_sha = existing.body.get("sha")
        existing_content = existing.body.get("content", "")
        # base64 with newlines per RFC 4648 §3.2 — decode + compare to skip
        # a no-op commit so reruns stay idempotent.
        try:
            current_bytes = base64.b64decode(existing_content)
            if current_bytes.decode("utf-8") == spec_body:
                log.emit(
                    "nothing-to-commit", path=spec_path, branch=branch,
                    note="file already up to date on branch",
                )
                return _ensure_pr(
                    repo=repo, branch=branch, base_branch=base_branch,
                    issue=issue, title=title, spec_path=spec_path,
                    log=log, token=token,
                )
        except (ValueError, UnicodeDecodeError):
            # Treat undecodable content as "different" and proceed with update.
            pass

    # 3. PUT the file. GitHub creates the branch if missing (off base_branch)
    # and produces a verified-signature commit because the actor is the App.
    put_body: dict[str, Any] = {
        "message": f"Spec Generator — draft spec for issue #{issue}",
        "content": base64.b64encode(spec_body.encode("utf-8")).decode("ascii"),
        "branch": branch,
        "committer": {"name": BOT_NAME, "email": _bot_email(app_id)},
    }
    if existing_sha:
        put_body["sha"] = existing_sha
    put_resp = _gh_request(
        "PUT", f"/repos/{repo}/contents/{spec_path}", token=token, body=put_body
    )
    put_resp_body = put_resp.body if isinstance(put_resp.body, dict) else {}
    commit_sha = str((put_resp_body.get("commit") or {}).get("sha", ""))[:8]
    log.emit(
        "committed-spec",
        branch=branch,
        path=spec_path,
        sha=commit_sha,
        verified=True,
    )

    # 4. Open the PR (or find the existing one).
    return _ensure_pr(
        repo=repo, branch=branch, base_branch=base_branch,
        issue=issue, title=title, spec_path=spec_path,
        log=log, token=token,
    )


def _ensure_pr(
    *,
    repo: str,
    branch: str,
    base_branch: str,
    issue: int,
    title: str,
    spec_path: str,
    log: EventLog,
    token: str,
) -> int | None:
    """Find or create the spec PR via the REST API."""
    existing = _gh_request(
        "GET",
        f"/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch}&state=open",
        token=token,
    )
    if isinstance(existing.body, list) and existing.body:
        pr_number = existing.body[0].get("number")
        log.emit("pr-exists", pr=pr_number, branch=branch)
        return int(pr_number) if pr_number else None

    body = (
        f"Spec Generator — drafted in response to issue #{issue}.\n\n"
        f"Spec: {spec_path}\n\n"
        f"This PR contains the design spec at `{spec_path}`.\n\n"
        f"Comment `/confirm` on issue #{issue} (or here) to advance the "
        f"`intent-confirmed` label and hand the spec off to the "
        f"Implementation Agent. If no questions land in the next 24 hours, "
        f"the sweep job will confirm automatically."
    )
    create = _gh_request(
        "POST",
        f"/repos/{repo}/pulls",
        token=token,
        body={
            "title": f"spec: {title}",
            "body": body,
            "head": branch,
            "base": base_branch,
        },
    )
    create_body = create.body if isinstance(create.body, dict) else {}
    pr_number = create_body.get("number")
    log.emit(
        "pr-created",
        pr=pr_number,
        url=create_body.get("html_url"),
    )
    return int(pr_number) if pr_number else None


def _extract_token(push_url: str | None) -> str | None:
    """Pull ``x-access-token:<token>@...`` out of a legacy push URL."""
    if not push_url or "x-access-token:" not in push_url:
        return None
    try:
        return push_url.split("x-access-token:", 1)[1].split("@", 1)[0]
    except IndexError:
        return None


def _extract_repo(push_url: str | None) -> str | None:
    """Pull ``owner/name`` out of ``https://...@github.com/owner/name.git``."""
    if not push_url:
        return None
    try:
        path = push_url.split("github.com/", 1)[1]
        return path.removesuffix(".git").strip("/")
    except IndexError:
        return None


# ---------------------------------------------------------------------------
# Legacy helpers retained so existing callers / tests that still reach for
# `_git` keep working. Marked private; they are not part of the public API.
# ---------------------------------------------------------------------------


def _git(
    workspace_root: str,
    *args: str,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Legacy git wrapper — kept for the unit-test surface that asserted on
    ``CalledProcessError`` propagation. Production no longer uses this path."""
    proc = subprocess.run(
        ["git", "-C", workspace_root, *args],
        check=False, capture_output=True, text=True, input=stdin,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd=list(proc.args) + ([f"# stderr: {stderr}"] if stderr else []),
            output=proc.stdout,
            stderr=stderr,
        )
    return proc


def _parse_pr_url(url: str) -> int | None:
    """`https://github.com/owner/repo/pull/123` -> 123. Kept for backwards-compat."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


# Silence unused-import warnings for `os` (kept available for future env reads).
_ = os
