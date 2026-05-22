"""Unit tests for the GitHub write-back + webhook handler (Phase D)."""

from agents.implementation.core import Orchestrator
from agents.implementation.github_io import (
    FakeGitHubClient,
    GitHubClient,
    handle_webhook,
    resolve_spec_path,
)
from agents.implementation.speckit_driver import SpecKitDriver
from agents.implementation.workspace import InMemoryWorkspace


def _issue_comment_payload(body: str, pr: int = 42) -> dict:
    return {
        "action": "created",
        "issue": {"number": pr, "pull_request": {"url": f"u/{pr}"}},
        "comment": {"body": body, "user": {"login": "reporter-alice"}},
    }


# -- intent_confirmed fixtures -------------------------------------------------
_SPEC_PATH = "docs/superpowers/specs/2026-05-22-widget-design.md"
_DESIGN_SPEC = (
    "# Widget — Design Spec\n\n## 1. Goal\nAdd a widget counter in the addon "
    "`custom-addons/widget`.\n"
)
_ACL_HEADER = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
)

# A clean /speckit.analyze report so planning proceeds (an empty report escalates).
CLEAN_ANALYZE = {
    "parts": [{"type": "text", "text": "Analysis complete. No inconsistencies found."}]
}


def _intent_confirmed_payload(pr: int = 17, branch: str = "agent/spec-1500") -> dict:
    return {
        "action": "labeled",
        "label": {"name": "intent-confirmed"},
        "pull_request": {"number": pr, "head": {"ref": branch}},
    }


def _clean_addon_files() -> dict:
    """A minimal, Odoo-rule-clean `custom-addons/widget` addon."""
    return {
        "custom-addons/widget/__manifest__.py": (
            "{'name': 'Widget', 'version': '19.0.1.0.0', 'depends': ['base'], "
            "'data': ['security/ir.model.access.csv'], 'license': 'LGPL-3'}"
        ),
        "custom-addons/widget/models/widget.py": (
            "class Widget(models.Model):\n    _name = 'widget.counter'\n"
        ),
        "custom-addons/widget/security/ir.model.access.csv": (
            _ACL_HEADER + "access_w,a,model_widget_counter,base.group_user,1,1,1,1\n"
        ),
    }


def test_fake_github_client_satisfies_the_protocol():
    assert isinstance(FakeGitHubClient(), GitHubClient)


def test_handle_webhook_runs_reporter_iteration_and_writes_back(fake_client):
    ws = InMemoryWorkspace()  # no saved session -> a change request escalates
    github = FakeGitHubClient(branches={42: "agent/spec-1500"})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook(
        "issue_comment",
        _issue_comment_payload("Please rename the field"),
        orch,
        github,
    )
    assert result is not None
    assert result.status == "escalated"
    assert ws.branch == "agent/spec-1500"          # the PR head branch was resolved
    assert github.comments and github.comments[0][0] == 42
    assert (42, "needs-human") in github.labels


def test_handle_webhook_noise_comment_writes_nothing(fake_client):
    ws = InMemoryWorkspace()
    github = FakeGitHubClient(branches={42: "agent/spec-1500"})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook(
        "issue_comment", _issue_comment_payload("thanks, appreciate it!"), orch, github
    )
    assert result is not None
    assert result.status == "ignored"
    assert github.comments == []
    assert github.labels == []


def test_handle_webhook_ignores_unrelated_webhooks(fake_client):
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    assert handle_webhook("star", {"action": "created"}, orch, github) is None
    assert github.comments == []
    assert github.labels == []


def test_handle_webhook_ignores_a_comment_with_no_pr_number(fake_client):
    """A malformed issue_comment payload (no PR number) is not actionable —
    handle_webhook must not run a coder iteration it cannot write back."""
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = {
        "action": "created",
        "issue": {"pull_request": {"url": "u"}},  # no "number"
        "comment": {"body": "Please rename the field", "user": {"login": "alice"}},
    }
    assert handle_webhook("issue_comment", payload, orch, github) is None
    assert github.comments == []
    assert github.labels == []
    assert fake_client.commands == []


def test_handle_webhook_intent_confirmed_runs_implement_and_writes_back(fake_client):
    """A labeled (intent-confirmed) PR runs the full implement flow, and the
    outcome is written back as a PR comment."""
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace({_SPEC_PATH: _DESIGN_SPEC, **_clean_addon_files()})
    github = FakeGitHubClient(changed_files={17: [_SPEC_PATH]})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook("pull_request", _intent_confirmed_payload(), orch, github)
    assert result is not None
    assert result.status == "implemented"
    assert github.comments and github.comments[0][0] == 17
    assert github.labels == []  # success — no escalation label
    assert "speckit.implement" in [c["command"] for c in fake_client.commands]


def test_handle_webhook_intent_confirmed_escalates_when_no_spec_resolves(fake_client):
    """When the PR's changed files contain no resolvable spec, the agent
    escalates instead of running implement."""
    github = FakeGitHubClient(changed_files={17: ["README.md", "Makefile"]})
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    result = handle_webhook("pull_request", _intent_confirmed_payload(), orch, github)
    assert result is None
    assert github.comments and github.comments[0][0] == 17
    assert (17, "needs-human") in github.labels
    assert fake_client.commands == []  # implement never ran


def test_handle_webhook_intent_confirmed_escalates_when_implement_escalates(
    fake_client,
):
    """An incoherent /analyze escalates the implement flow — handle_webhook
    writes the escalation back as a comment + label."""
    fake_client.set_command_result(
        "speckit.analyze",
        {"parts": [{"type": "text", "text": "CRITICAL: spec contradicts the model"}]},
    )
    ws = InMemoryWorkspace({_SPEC_PATH: _DESIGN_SPEC})
    github = FakeGitHubClient(changed_files={17: [_SPEC_PATH]})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook("pull_request", _intent_confirmed_payload(), orch, github)
    assert result is not None
    assert result.status == "escalated"
    assert github.comments
    assert (17, "needs-human") in github.labels


def test_handle_webhook_intent_confirmed_escalation_reports_the_odoo_findings(
    fake_client,
):
    """A coder-stage escalation (Odoo validation fails) writes the actual
    failing Odoo rule into the escalation comment — not a generic fallback."""
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    files = _clean_addon_files()
    # Drop the model's ACL row — coder validation fails, escalating at the
    # implement stage (planning succeeds, so planning carries no findings).
    files["custom-addons/widget/security/ir.model.access.csv"] = _ACL_HEADER
    ws = InMemoryWorkspace({_SPEC_PATH: _DESIGN_SPEC, **files})
    github = FakeGitHubClient(changed_files={17: [_SPEC_PATH]})
    orch = Orchestrator(ws, SpecKitDriver(fake_client))
    result = handle_webhook("pull_request", _intent_confirmed_payload(), orch, github)
    assert result is not None
    assert result.status == "escalated"
    assert (17, "needs-human") in github.labels
    body = github.comments[0][1]
    assert "security.missing_acl" in body
    assert "could not be completed automatically" not in body


def test_handle_webhook_intent_confirmed_ignores_a_payload_with_no_pr(fake_client):
    """A labeled-PR payload with no PR number is not actionable."""
    github = FakeGitHubClient()
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = {
        "action": "labeled",
        "label": {"name": "intent-confirmed"},
        "pull_request": {"head": {"ref": "agent/spec-1500"}},  # no "number"
    }
    assert handle_webhook("pull_request", payload, orch, github) is None
    assert github.comments == []
    assert github.labels == []
    assert fake_client.commands == []


def test_fake_github_client_returns_canned_changed_files():
    github = FakeGitHubClient(changed_files={5: ["a.py", "b.md"]})
    assert github.pr_changed_files(5) == ["a.py", "b.md"]
    assert github.pr_changed_files(99) == []


def test_resolve_spec_path_picks_the_single_design_spec():
    files = [_SPEC_PATH, "custom-addons/widget/__manifest__.py"]
    assert resolve_spec_path(files) == _SPEC_PATH


def test_resolve_spec_path_picks_a_fix_brief():
    fix = "docs/superpowers/specs/2026-05-22-login-fix.md"
    assert resolve_spec_path([fix, "custom-addons/auth/models/auth.py"]) == fix


def test_resolve_spec_path_is_none_when_no_spec_changed():
    assert resolve_spec_path(["README.md", "custom-addons/x/models/x.py"]) is None


def test_resolve_spec_path_is_none_when_two_specs_changed():
    files = [
        "docs/superpowers/specs/2026-05-22-a-design.md",
        "docs/superpowers/specs/2026-05-22-b-design.md",
    ]
    assert resolve_spec_path(files) is None


def test_resolve_spec_path_ignores_the_template_files():
    assert resolve_spec_path(["docs/superpowers/specs/_TEMPLATE-design.md"]) is None


# -- human-commit flow ---------------------------------------------------------
def _push_payload(
    branch: str = "agent/spec-1500", sha: str = "abc1234567", login: str = "lead-dev"
) -> dict:
    return {
        "ref": f"refs/heads/{branch}",
        "after": sha,
        "sender": {"login": login},
        "pusher": {"name": login},
    }


def test_fake_github_client_resolves_a_pr_by_branch():
    github = FakeGitHubClient(prs={"agent/spec-1500": 17})
    assert github.pr_for_branch("agent/spec-1500") == 17
    assert github.pr_for_branch("agent/spec-9999") is None


def test_handle_webhook_human_push_pings_the_reporter(fake_client):
    """A human push to the agent branch posts a re-confirm ping on the PR."""
    github = FakeGitHubClient(prs={"agent/spec-1500": 17})
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    handle_webhook("push", _push_payload(), orch, github)
    assert github.comments and github.comments[0][0] == 17
    body = github.comments[0][1]
    assert "lead-dev" in body
    assert "abc12345" in body  # the short sha


def test_handle_webhook_human_push_with_no_open_pr_posts_nothing(fake_client):
    github = FakeGitHubClient()  # no branch -> PR mapping
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    assert handle_webhook("push", _push_payload(), orch, github) is None
    assert github.comments == []


def test_handle_webhook_ignores_a_bot_push(fake_client):
    """The agent's own push is not a human commit — no ping."""
    github = FakeGitHubClient(prs={"agent/spec-1500": 17})
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = _push_payload(login="implementation-bot")
    assert handle_webhook("push", payload, orch, github) is None
    assert github.comments == []


def test_handle_webhook_ignores_a_github_app_bot_push(fake_client):
    """A GitHub App pushes as '<name>[bot]' — still the agent's own commit."""
    github = FakeGitHubClient(prs={"agent/spec-1500": 17})
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = _push_payload(login="implementation-bot[bot]")
    assert handle_webhook("push", payload, orch, github) is None
    assert github.comments == []


def test_handle_webhook_human_push_without_a_sha_still_pings(fake_client):
    """A push payload missing `after` still pings — the ping omits the sha."""
    github = FakeGitHubClient(prs={"agent/spec-1500": 17})
    orch = Orchestrator(InMemoryWorkspace(), SpecKitDriver(fake_client))
    payload = {"ref": "refs/heads/agent/spec-1500", "sender": {"login": "lead-dev"}}
    handle_webhook("push", payload, orch, github)
    assert github.comments and github.comments[0][0] == 17
