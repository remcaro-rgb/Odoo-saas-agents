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
        if _apply_one_modify(workspace_root, path, patch, entry):
            applied += 1
    return applied


def _apply_one_modify(
    workspace_root: str,
    path: str,
    patch: str,
    entry: dict[str, Any],
) -> bool:
    """Apply ONE modify patch independently. Returns True if landed (or
    already-in-tree), False if phantom-skipped.

    Per-patch (not blob) for resilience to OpenCode's session-diff baseline
    quirk: the session diff's BEFORE side reflects /workspace state when
    the LLM's first step started (PRE-provisioning leftovers from any
    previous session), NOT the PR head SHA the Action runner checked out.
    A blob-apply aborts atomically on any single hunk mismatch, so one
    phantom patch from pre-provisioning leftovers used to take legitimate
    co-bundled patches down with it. Per-patch lets the legit ones land
    while the phantom ones skip with a warning.

    Strategy: reset path to HEAD, try forward apply; on fail try
    reverse-check (idempotent already-applied case); on both fail log +
    skip. Never raises — phantom patches are an expected outcome of
    OpenCode's diff semantics, not a fatal error.
    """
    # Reset the file to HEAD so the patch lands on a clean baseline. Works
    # only for tracked files; brand-new paths error silently (check=False).
    _git(workspace_root, "checkout", "HEAD", "--", path, check=False)
    blob = patch + "\n"
    try:
        # `-p0` keeps paths as-is — OpenCode's headers carry no `a/`/`b/`
        # prefix. See the module-level docstring + commit `b55f340`.
        _git(workspace_root, "apply", "-p0", "--whitespace=nowarn", "-", stdin=blob)
        return True
    except subprocess.CalledProcessError:
        pass
    # Forward apply failed. Idempotent already-applied? Reverse-check.
    try:
        _git(
            workspace_root, "apply", "-p0", "--reverse", "--check", "-",
            stdin=blob,
        )
        return True  # Patch already in tree — no-op, but count as landed.
    except subprocess.CalledProcessError:
        pass
    # Both directions failed → phantom patch (BEFORE doesn't match HEAD,
    # AFTER doesn't match current state). The agent's intended change for
    # this file isn't recoverable from this cumulative session diff.
    # Log to stderr (workflow log) and skip — do NOT raise. Verified on
    # PR #37 run 26348556180: agent's diff for models/account_ledger_report.py
    # expected `tools.SQL(...)` BEFORE, but HEAD has `"""..."""`. Pre-
    # provisioning leftovers from a prior session anchored the diff
    # baseline.
    _dump_apply_failure_state(workspace_root, [entry], blob)
    return False


def _dump_apply_failure_state(
    workspace_root: str,
    diffs: list[dict[str, Any]],
    blob: str,
) -> None:
    """Print a diagnostic when `git apply` rejects a patch — the on-disk
    bytes of every target file, side-by-side with the patch's expected
    context. Identifies line-ending / whitespace / EOL differences that
    git's `patch failed: <file>:1` doesn't reveal on its own.

    No-op-safe (best-effort): a failure here must not mask the original
    CalledProcessError, so the whole body is wrapped in try/except.
    """
    import hashlib
    import sys
    try:
        sys.stderr.write("\n=== pushback apply failure diagnostic ===\n")
        sys.stderr.write(f"workspace_root={workspace_root}\n")
        sys.stderr.write(f"blob bytes={len(blob)} sha1={hashlib.sha1(blob.encode()).hexdigest()}\n")
        for entry in diffs:
            path = str(entry.get("file") or "")
            target = os.path.join(workspace_root, path)
            status = entry.get("status")
            on_disk = "MISSING"
            sha = "-"
            head_repr = ""
            if os.path.exists(target):
                with open(target, "rb") as fh:
                    raw = fh.read()
                sha = hashlib.sha1(raw).hexdigest()
                head_repr = repr(raw[:160])
                on_disk = f"{len(raw)} bytes"
            sys.stderr.write(
                f"  - {path} status={status!r} disk={on_disk} sha1={sha}\n"
                f"    head={head_repr}\n"
            )
        sys.stderr.write("--- blob (first 800 bytes) ---\n")
        sys.stderr.write(blob[:800])
        sys.stderr.write("\n=== end diagnostic ===\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001  pragma: no cover
        # Best-effort: never mask the apply error.
        pass


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
    # `actions/checkout@v4` with `persist-credentials: true` (the default)
    # installs a global `http.https://github.com/.extraheader` carrying the
    # workflow's default `github-actions[bot]` token. That extraheader
    # OVERRIDES the embedded `https://x-access-token:<App token>@github.com/`
    # in our push_url — git reaches the server with the github-actions[bot]
    # token, which lacks Contents:write, and the push returns
    # `remote: Permission to <repo>.git denied to github-actions[bot]`.
    # Suppressing the extraheader for this one command (via `-c`) lets the
    # App installation token in the URL win. Verified live 2026-05-24 on
    # PR #36 run 26347048257.
    _git(
        workspace_root,
        "-c", "http.https://github.com/.extraheader=",
        "push", push_url, f"HEAD:{branch}",
    )
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
