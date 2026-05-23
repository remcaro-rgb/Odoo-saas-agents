"""Push the implementation back to the PR branch (Tier 2 — design §10.1).

The agent drives `/speckit.implement` inside the headless OpenCode container;
the files OpenCode writes live in *its* workspace, not on GitHub. This module
closes that loop:

1. Fetch OpenCode's session diff (`OpenCodeClient.get_diff` -> a
   `SnapshotFileDiff[]`: `{file, patch, additions, deletions, status}`).
2. Apply the concatenated unified-diff `patch` payloads to the Action runner's
   data-plane checkout via `git apply`.
3. Commit as the `implementation-bot[bot]` identity (the GitHub App noreply
   email scheme — `<app_id>+implementation-bot[bot]@users.noreply.github.com`).
4. Push to the PR head branch using a short-lived App installation token in the
   URL, so the GitHub-side sender is the App (matches `_BOT_LOGINS`, so the
   push-notify workflow correctly treats it as the agent's own commit).

Shadow-aware: in SHADOW, `push_implementation` records what it *would* have
done in the event log and returns `None` without touching the worktree or
remote — that's what makes SHADOW genuinely "post / push nothing".

Requires the implementation-bot GitHub App's **Contents** permission to be
**Read & write** on the data-plane repo (TIER-1-RUNBOOK §5 step 2).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Protocol

from .observability import EventLog
from .provisioning import is_protected_path
from .rollout import RolloutDecision

BOT_NAME = "implementation-bot[bot]"


def _extract_added_content(patch: str) -> str:
    """Pull the new-file content out of an OpenCode-style ADD unified diff.

    OpenCode's shadow-git emits ADDs with ``--- <path>`` / ``+++ <path>``
    (NO ``/dev/null`` marker — git apply would interpret that as "modify a
    missing file"), so we cannot let `git apply` handle them: when bundled
    with other failing patches in the same `git apply` call (git apply is
    atomic over its input), the ADD silently never lands. Strip the headers
    and hunk markers, take every ``+``-prefixed line as content.

    Tolerates the trailing-tab `\\t` that OpenCode appends to the
    ``--- `` / ``+++ `` headers and the SVN-style ``Index:`` / ``===``
    preamble. Skips the ``+++`` header itself (which starts with ``+``
    but is not content).
    """
    out: list[str] = []
    in_body = False
    for line in patch.split("\n"):
        if not in_body:
            if line.startswith("@@"):
                in_body = True
            continue
        if line.startswith("+"):
            # `+++` headers don't appear in the body — they live above the
            # first `@@`. Inside the body, any `+`-prefixed row is content.
            out.append(line[1:])
        # ` ` (context) and `-` (removal) rows are not present in an ADD
        # but we'd skip them anyway — only `+` lines carry new content.
    return "\n".join(out)


class _DiffClient(Protocol):
    """The OpenCode-client surface this module uses (duck-typed for tests)."""

    def get_diff(
        self, session_id: str, *, message_id: str | None = None
    ) -> list[dict[str, Any]]: ...


def _git(
    workspace_root: str, *args: str, check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `git -C <workspace_root> <args>`. On failure, raise
    ``CalledProcessError`` with git's stderr folded into the exception
    message — the default repr is just `"Command '[...]' returned non-zero
    exit status N"` which leaves you guessing about which `fatal:` actually
    fired (especially for credential-masked push URLs)."""
    proc = subprocess.run(
        ["git", "-C", workspace_root, *args],
        check=False, capture_output=True, text=True, input=stdin,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise subprocess.CalledProcessError(
            proc.returncode,
            # Surface stderr by jamming it into the command repr; the
            # CalledProcessError default __str__ prints `Command '<cmd>'
            # returned non-zero exit status N` and that's the only thing
            # GitHub Actions logs from a Python traceback.
            cmd=list(proc.args) + ([f"# stderr: {stderr}"] if stderr else []),
            output=proc.stdout,
            stderr=stderr,
        )
    return proc


def apply_session_diff(
    workspace_root: str, diffs: list[dict[str, Any]]
) -> int:
    """Apply OpenCode's `SnapshotFileDiff[]` to `workspace_root`.

    Two paths, split on the entry's ``status``:

    * ``status == "added"`` — extract the new-file content from the patch
      (``+``-prefixed body) and write it directly. OpenCode's ADD patches
      use ``--- <path>`` / ``+++ <path>`` (no ``/dev/null`` marker), which
      ``git apply`` interprets as "modify a missing file" and rejects.
      Writing directly also sidesteps git apply's atomicity — when bundled
      with a failing modify patch, the ADD would never land otherwise.
    * everything else (``"modified"``, missing status) — collected into one
      unified-diff blob, applied via ``git apply -p0``. ``-p0`` because
      OpenCode's headers carry no ``a/`` / ``b/`` prefix.

    Returns the number of file entries acted on (write + apply combined).
    Entries with no ``patch`` (only a summary) are silently skipped — this
    happens for diffs OpenCode tracks at metadata level but cannot
    reconstruct as a patch. Protected guardrail paths are filtered out as
    defence in depth on top of the container's sparse-checkout.
    """
    if not diffs:
        return 0
    pieces: list[str] = []
    applied = 0
    for entry in diffs:
        path = str(entry.get("file") or "")
        # Defence in depth: never apply a diff that targets a guardrail path,
        # even if OpenCode somehow surfaced one. The container's sparse-checkout
        # in provisioning.py is the first line of defence.
        if is_protected_path(path):
            continue
        patch = str(entry.get("patch", "")).rstrip("\n")
        if not patch.strip():
            continue
        status = str(entry.get("status", "")).lower()
        if status == "added":
            # Write the new file directly — bypassing `git apply` (see
            # docstring + `_extract_added_content`).
            target = os.path.join(workspace_root, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            content = _extract_added_content(patch)
            # OpenCode includes the trailing newline that the file had on
            # disk; if the body has it, keep it. Patches for files with no
            # trailing newline mark that with `\ No newline at end of file`
            # which `_extract_added_content` does NOT propagate — write as-is.
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content + ("\n" if not content.endswith("\n") else ""))
            applied += 1
            continue
        pieces.append(patch)
        applied += 1
    if not pieces:
        return applied
    blob = "\n".join(pieces) + "\n"
    try:
        # `-p0` keeps paths as-is. OpenCode's shadow-git unified diffs lack
        # the `a/` / `b/` header prefixes that `git diff` emits, so the
        # default `-p1` strips the first segment off (e.g.
        # `custom-addons/x/__manifest__.py` -> `x/__manifest__.py`) and the
        # apply fails with `No such file or directory`. Verified live
        # 2026-05-23 against probe session ses_1a8f4c29effeUMS10Ho63UvuQ9
        # (Tier-7 follow-up).
        _git(workspace_root, "apply", "-p0", "--whitespace=nowarn", "-", stdin=blob)
    except subprocess.CalledProcessError:
        # Likely already applied — `Coder._sync_from_session` runs the same
        # apply inside the implement loop, then `push_implementation` here
        # re-fetches `get_diff` and tries again. Confirm via reverse-check;
        # if the patch IS already in the tree, the diff is a no-op here.
        # Otherwise the patch is genuinely bad and we re-raise.
        _git(workspace_root, "apply", "-p0", "--reverse", "--check", "-", stdin=blob)
    return applied


def commit_and_push(
    workspace_root: str,
    branch: str,
    push_url: str,
    *,
    message: str,
    app_id: str | None = None,
) -> str | None:
    """Stage everything, commit as the bot, push to `branch`.

    Returns the new HEAD SHA, or `None` when there is nothing to commit (the
    diff applied cleanly but resulted in zero staged changes — for example,
    a patch that idempotently re-asserts existing content).
    """
    email = (
        f"{app_id}+implementation-bot[bot]@users.noreply.github.com"
        if app_id
        else "implementation-bot[bot]@users.noreply.github.com"
    )
    _git(workspace_root, "config", "user.email", email)
    _git(workspace_root, "config", "user.name", BOT_NAME)
    _git(workspace_root, "add", "-A")
    if _git(workspace_root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(workspace_root, "commit", "-m", message)
    _git(workspace_root, "push", push_url, f"HEAD:{branch}")
    return _git(workspace_root, "rev-parse", "HEAD").stdout.strip()


def push_implementation(
    *,
    workspace_root: str,
    oc_client: _DiffClient,
    session_id: str,
    branch: str,
    push_url: str,
    feature: str,
    decision: RolloutDecision,
    log: EventLog,
    app_id: str | None = None,
) -> str | None:
    """Fetch OpenCode's session diff and push it to the PR head branch.

    Returns the pushed SHA on a real push; `None` for SHADOW, an empty diff,
    or a diff that applied to zero staged changes.
    """
    diffs = oc_client.get_diff(session_id) or []
    if decision is RolloutDecision.SHADOW:
        log.emit(
            "push.shadowed",
            session_id=session_id,
            branch=branch,
            files=sum(1 for d in diffs if d.get("file")),
        )
        return None
    applied = apply_session_diff(workspace_root, diffs)
    if applied == 0:
        log.emit("push.no-diff", session_id=session_id)
        return None
    sha = commit_and_push(
        workspace_root, branch, push_url,
        message=f"[impl-agent] {feature}: implement spec",
        app_id=app_id,
    )
    log.emit(
        "push.committed" if sha else "push.no-changes",
        session_id=session_id, branch=branch, sha=sha, files=applied,
    )
    return sha
