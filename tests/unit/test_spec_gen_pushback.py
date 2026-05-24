"""Unit tests for spec-generator pushback (Tier 2, Contents-API).

`push_spec` was refactored from `git commit`+`git push` to use the
GitHub Contents API so commits are server-signed by the App's verified
key (required by the `agent-spec-branches` ruleset on the data plane).

These tests mock `urllib.request.urlopen` so the suite stays fully offline.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from agents.implementation.observability import EventLog
from agents.implementation.rollout import RolloutDecision
from agents.spec_generator import pushback
from agents.spec_generator.pushback import _extract_repo, _extract_token, push_spec


class _FakeResp:
    def __init__(self, status: int, body: Any = None):
        self.status = status
        self._payload = (
            json.dumps(body).encode("utf-8") if body is not None else b""
        )

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def fake_gh(monkeypatch):
    """Replace urllib.request.urlopen with a programmable scripted responder."""
    calls: list[dict[str, Any]] = []
    script: list[Any] = []

    def _urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = (
            req.get_method() if hasattr(req, "get_method") else "GET"
        )
        body = None
        if hasattr(req, "data") and req.data:
            body = json.loads(req.data.decode("utf-8"))
        calls.append({"method": method, "url": url, "body": body})
        if not script:
            raise RuntimeError(f"unexpected call: {method} {url}")
        next_resp = script.pop(0)
        if isinstance(next_resp, BaseException):
            raise next_resp
        return next_resp

    monkeypatch.setattr(pushback.urllib.request, "urlopen", _urlopen)
    return calls, script


def test_shadow_short_circuits_before_any_http(tmp_path, fake_gh):
    calls, _ = fake_gh
    log = EventLog()
    pr = push_spec(
        workspace_root=str(tmp_path),
        spec_path="docs/specs/x-design.md",
        spec_body="# spec body",
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.SHADOW,
        log=log,
    )
    assert pr is None
    assert calls == []  # zero HTTP calls in SHADOW
    assert any(r["event"] == "shadow-push" for r in log.records)


def test_act_creates_file_via_contents_api_and_opens_pr(fake_gh):
    calls, script = fake_gh
    # 1) GET /repos/o/r/branches/main -> base sha
    script.append(_FakeResp(200, {"commit": {"sha": "basesha123456"}}))
    # 1b) POST /repos/o/r/git/refs -> branch created (201)
    script.append(_FakeResp(201, {"ref": "refs/heads/agent/spec-0001-x"}))
    # 2) GET contents -> 404 (file doesn't exist yet)
    script.append(HTTPError("u", 404, "missing", {}, io.BytesIO(b"{}")))
    # 3) PUT contents -> commit landed (server-signed)
    script.append(_FakeResp(201, {"commit": {"sha": "newcommit123"}}))
    # 4) GET pulls -> empty
    script.append(_FakeResp(200, []))
    # 5) POST pulls -> PR opened
    script.append(_FakeResp(
        201, {"number": 42, "html_url": "https://github.com/o/r/pull/42"}
    ))

    log = EventLog()
    pr = push_spec(
        workspace_root="/tmp/unused",
        spec_path="docs/superpowers/specs/0001-x-design.md",
        spec_body="# Spec body\n\nSome content.\n",
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:bot-token-xyz@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
        app_id="999",
    )
    assert pr == 42
    # 6 HTTP calls in the expected order — note the new POST /git/refs.
    methods = [(c["method"], c["url"].split("/repos/o/r")[1].split("?")[0]) for c in calls]
    assert methods[0] == ("GET", "/branches/main")
    assert methods[1] == ("POST", "/git/refs")
    assert methods[2] == ("GET", "/contents/docs/superpowers/specs/0001-x-design.md")
    assert methods[3] == ("PUT", "/contents/docs/superpowers/specs/0001-x-design.md")
    assert methods[4] == ("GET", "/pulls")
    assert methods[5] == ("POST", "/pulls")
    # POST /git/refs body references the agent branch off base sha.
    refs_body = calls[1]["body"]
    assert refs_body["ref"] == "refs/heads/agent/spec-0001-x"
    assert refs_body["sha"] == "basesha123456"
    # PUT body has base64-encoded content + branch + committer.
    put_body = calls[3]["body"]
    assert put_body["branch"] == "agent/spec-0001-x"
    decoded = base64.b64decode(put_body["content"]).decode("utf-8")
    assert decoded == "# Spec body\n\nSome content.\n"
    assert put_body["committer"]["email"].startswith("999+spec-generator-bot[bot]@")
    # Log shows the verified commit + PR.
    assert any(r["event"] == "branch-created" for r in log.records)
    assert any(r["event"] == "committed-spec" and r.get("verified") for r in log.records)
    assert any(r["event"] == "pr-created" and r.get("pr") == 42 for r in log.records)


def test_act_tolerates_branch_already_exists(fake_gh):
    """422 on POST /git/refs is the idempotent case for a re-run."""
    calls, script = fake_gh
    script.append(_FakeResp(200, {"commit": {"sha": "basesha"}}))
    # POST /git/refs -> 422 because the branch already exists from prior run
    script.append(HTTPError("u", 422, "Reference already exists", {}, io.BytesIO(b"{}")))
    # GET contents -> 200 + same content (idempotent path)
    spec_body = "# spec body"
    script.append(_FakeResp(200, {
        "sha": "blobsha",
        "content": base64.b64encode(spec_body.encode("utf-8")).decode("ascii"),
    }))
    # GET pulls -> existing PR
    script.append(_FakeResp(200, [{"number": 5}]))

    log = EventLog()
    pr = push_spec(
        workspace_root="/tmp/x",
        spec_path="docs/specs/x-design.md",
        spec_body=spec_body,
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    assert pr == 5
    assert any(r["event"] == "branch-exists" for r in log.records)


def test_act_updates_existing_file_with_blob_sha(fake_gh):
    calls, script = fake_gh
    script.append(_FakeResp(200, {"commit": {"sha": "basesha"}}))
    # POST /git/refs -> branch already exists (422)
    script.append(HTTPError("u", 422, "exists", {}, io.BytesIO(b"{}")))
    # File exists -> 200 + blob SHA + base64 content.
    script.append(_FakeResp(200, {
        "sha": "blobsha789",
        "content": base64.b64encode(b"# old content").decode("ascii"),
    }))
    script.append(_FakeResp(200, {"commit": {"sha": "updatedsha"}}))
    script.append(_FakeResp(200, []))
    script.append(_FakeResp(201, {"number": 7, "html_url": "..."}))

    log = EventLog()
    push_spec(
        workspace_root="/tmp/x",
        spec_path="docs/specs/x-design.md",
        spec_body="# new content",  # different from existing
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    # PUT body included the blob SHA (the API requires it for updates).
    # Index 3 because: 0=GET branches, 1=POST refs, 2=GET contents, 3=PUT contents
    assert calls[3]["body"]["sha"] == "blobsha789"


def test_act_idempotent_when_existing_content_matches(fake_gh):
    calls, script = fake_gh
    spec_body = "# spec body"
    script.append(_FakeResp(200, {"commit": {"sha": "basesha"}}))
    # POST /git/refs -> 422 (branch already exists)
    script.append(HTTPError("u", 422, "exists", {}, io.BytesIO(b"{}")))
    # Existing file content matches what we'd write — no commit, just find/open PR.
    script.append(_FakeResp(200, {
        "sha": "blobsha",
        "content": base64.b64encode(spec_body.encode("utf-8")).decode("ascii"),
    }))
    # No PUT expected; next call is the GET /pulls to find the existing PR.
    script.append(_FakeResp(200, [{"number": 42, "html_url": "..."}]))

    log = EventLog()
    pr = push_spec(
        workspace_root="/tmp/x",
        spec_path="docs/specs/x-design.md",
        spec_body=spec_body,
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    assert pr == 42
    # No PUT call was made — the third call was GET /pulls, not PUT.
    methods = [c["method"] for c in calls]
    assert "PUT" not in methods
    assert any(r["event"] == "nothing-to-commit" for r in log.records)


def test_act_returns_existing_pr_when_one_is_already_open(fake_gh):
    calls, script = fake_gh
    script.append(_FakeResp(200, {"commit": {"sha": "basesha"}}))
    script.append(_FakeResp(201, {"ref": "refs/heads/agent/spec-0001-x"}))  # new branch
    script.append(HTTPError("u", 404, "missing", {}, io.BytesIO(b"{}")))
    script.append(_FakeResp(201, {"commit": {"sha": "newsha"}}))
    script.append(_FakeResp(200, [{"number": 99}]))  # one open PR

    log = EventLog()
    pr = push_spec(
        workspace_root="/tmp/x",
        spec_path="docs/specs/x-design.md",
        spec_body="content",
        branch="agent/spec-0001-x",
        push_url="https://x-access-token:tk@github.com/o/r.git",
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    assert pr == 99
    assert any(r["event"] == "pr-exists" for r in log.records)


def test_act_skips_when_no_token_available(fake_gh):
    calls, _ = fake_gh
    log = EventLog()
    pr = push_spec(
        workspace_root="/tmp/x",
        spec_path="docs/specs/x-design.md",
        spec_body="x",
        branch="agent/spec-1-x",
        push_url="https://example.invalid",  # no x-access-token in URL
        issue=1,
        title="x",
        decision=RolloutDecision.ACT,
        log=log,
    )
    assert pr is None
    assert calls == []
    assert any(r["event"] == "push-skipped" for r in log.records)


def test_extract_token_recovers_token_from_push_url():
    url = "https://x-access-token:tok-abc@github.com/o/r.git"
    assert _extract_token(url) == "tok-abc"


def test_extract_token_returns_none_for_unsigned_url():
    assert _extract_token("https://example.com/foo.git") is None


def test_extract_repo_strips_dot_git():
    assert (
        _extract_repo("https://x:tk@github.com/owner/repo.git") == "owner/repo"
    )


def test_extract_repo_handles_missing_dot_git():
    assert _extract_repo("https://x:tk@github.com/owner/repo") == "owner/repo"
