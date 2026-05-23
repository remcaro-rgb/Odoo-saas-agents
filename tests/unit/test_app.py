"""Unit tests for the composition root + entry point (Tier 1 — make it runnable)."""

import json

from agents.implementation.app import (
    AgentConfig,
    build_github,
    build_notifier,
    build_orchestrator,
    load_event,
    main,
    notify_outcome,
    rollout_target,
    run,
)
from agents.implementation.classifier import CommentIntent
from agents.implementation.core import (
    FlowResult,
    IterationResult,
    Orchestrator,
    PlanningResult,
)
from agents.implementation.gate1 import Gate1
from agents.implementation.github_io import GhCliClient, ShadowGitHubClient
from agents.implementation.notifier import FakeNotifier, SlackNotifier
from agents.implementation.observability import EventLog
from agents.implementation.opencode_client import DEFAULT_BASE_URL
from agents.implementation.rollout import RolloutDecision


def _log() -> EventLog:
    """An EventLog with a silent sink — tests inspect `.records`, not stdout."""
    return EventLog(sink=lambda line: None)


def _events(log: EventLog) -> list[str]:
    return [record["event"] for record in log.records]


def _event_env(tmp_path, event_name: str, payload: dict, **extra: str) -> dict:
    """An env dict pointing at a written event-payload file, like GitHub Actions."""
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(path),
        **extra,
    }


# -- AgentConfig ---------------------------------------------------------------
def test_agent_config_from_env_uses_safe_defaults():
    config = AgentConfig.from_env({})
    assert config.opencode_base_url == DEFAULT_BASE_URL
    assert config.opencode_password is None
    assert config.data_plane_repo is None
    assert config.workspace_root == "."
    assert config.gate1_enabled is False


def test_agent_config_from_env_reads_overrides():
    config = AgentConfig.from_env(
        {
            "OPENCODE_BASE_URL": "https://oc.example",
            "OPENCODE_SERVER_PASSWORD": "s3cret",
            "DATA_PLANE_REPO": "acme/odoo",
            "WORKSPACE_ROOT": "/checkout",
            "GATE1_ENABLED": "true",
            "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/X/Y/Z",
        }
    )
    assert config.opencode_base_url == "https://oc.example"
    assert config.opencode_password == "s3cret"
    assert config.data_plane_repo == "acme/odoo"
    assert config.workspace_root == "/checkout"
    assert config.gate1_enabled is True
    assert config.slack_webhook_url == "https://hooks.slack.com/services/X/Y/Z"


def test_agent_config_slack_webhook_url_defaults_to_none():
    assert AgentConfig.from_env({}).slack_webhook_url is None


def test_agent_config_reads_bot_token_and_app_id_for_pushback():
    """The push step needs the App's installation token (`GH_TOKEN` in the
    workflow env) + the App id (for the canonical bot noreply email)."""
    config = AgentConfig.from_env(
        {"GH_TOKEN": "ghs_inst_token", "IMPLEMENTATION_BOT_APP_ID": "3818388"}
    )
    assert config.bot_token == "ghs_inst_token"
    assert config.app_id == "3818388"
    bare = AgentConfig.from_env({})
    assert bare.bot_token is None
    assert bare.app_id is None


# -- load_event ----------------------------------------------------------------
def test_load_event_reads_the_name_and_payload(tmp_path):
    env = _event_env(tmp_path, "push", {"ref": "refs/heads/agent/spec-1"})
    loaded = load_event(env)
    assert loaded is not None
    name, payload = loaded
    assert name == "push"
    assert payload == {"ref": "refs/heads/agent/spec-1"}


def test_load_event_is_none_outside_github_actions():
    assert load_event({}) is None
    assert load_event({"GITHUB_EVENT_NAME": "push"}) is None  # path missing


# -- rollout_target ------------------------------------------------------------
def test_rollout_target_is_the_pr_number_for_a_labeled_pr():
    payload = {"pull_request": {"number": 17, "head": {"ref": "agent/spec-1500"}}}
    assert rollout_target("pull_request", payload) == "17"


def test_rollout_target_is_the_issue_number_for_a_comment():
    payload = {"issue": {"number": 42, "pull_request": {"url": "u"}}}
    assert rollout_target("issue_comment", payload) == "42"


def test_rollout_target_is_the_branch_for_a_push():
    payload = {"ref": "refs/heads/agent/spec-1500"}
    assert rollout_target("push", payload) == "agent/spec-1500"


def test_rollout_target_falls_back_to_the_repo_full_name():
    payload = {"repository": {"full_name": "acme/odoo"}}
    assert rollout_target("star", payload) == "acme/odoo"


# -- build_orchestrator --------------------------------------------------------
def test_build_orchestrator_wires_the_repo_and_no_gate_by_default(fake_client):
    config = AgentConfig.from_env({"DATA_PLANE_REPO": "acme/odoo"})
    orch = build_orchestrator(config, fake_client)
    assert isinstance(orch, Orchestrator)
    assert orch.repo == "acme/odoo"
    assert orch.gate1 is None  # Gate 1 needs agentlab — a later (Tier 2) seam


def test_build_orchestrator_wires_gate1_when_enabled(fake_client):
    config = AgentConfig.from_env(
        {"DATA_PLANE_REPO": "acme/odoo", "GATE1_ENABLED": "true"}
    )
    orch = build_orchestrator(config, fake_client)
    assert isinstance(orch.gate1, Gate1)


def test_build_orchestrator_gate1_lint_only_by_default(fake_client):
    """`GATE1_CHECK_SET` defaults to `lint`: Gate 1 runs only `ruff check`. The
    Action runner has no Odoo / Postgres, so build / tests would always fail
    in vanilla CI — defer those to the agentlab-SSH path (§7)."""
    config = AgentConfig.from_env(
        {"DATA_PLANE_REPO": "acme/odoo", "GATE1_ENABLED": "true"}
    )
    orch = build_orchestrator(config, fake_client)
    assert orch.gate1 is not None
    names = [name for name, _ in orch.gate1.checks]
    assert names == ["lint"]


def test_build_orchestrator_gate1_full_set_keeps_all_three_checks(fake_client):
    """`GATE1_CHECK_SET=full` restores the build + tests checks for the future
    agentlab-SSH runner; the lint check stays first."""
    config = AgentConfig.from_env(
        {
            "DATA_PLANE_REPO": "acme/odoo",
            "GATE1_ENABLED": "true",
            "GATE1_CHECK_SET": "full",
        }
    )
    orch = build_orchestrator(config, fake_client)
    assert orch.gate1 is not None
    names = [name for name, _ in orch.gate1.checks]
    assert names == ["lint", "build", "tests"]


def test_agent_config_reads_gate1_check_set_with_default_lint():
    assert AgentConfig.from_env({}).gate1_check_set == "lint"
    assert (
        AgentConfig.from_env({"GATE1_CHECK_SET": "full"}).gate1_check_set == "full"
    )


# -- build_orchestrator: shadow-aware provisioning (Tier-3) --------------------
def test_build_orchestrator_default_is_shadow_mode(fake_client):
    """The default decision is SHADOW — callers must opt INTO ACT explicitly.
    Safer than the inverse: if someone forgets to thread the rollout
    decision through, the orchestrator still suppresses the container's
    autonomous push (Tier-3 / runbook §7)."""
    config = AgentConfig.from_env({"DATA_PLANE_REPO": "acme/odoo"})
    orch = build_orchestrator(config, fake_client)
    assert orch.shadow is True


def test_build_orchestrator_act_decision_disables_shadow(fake_client):
    config = AgentConfig.from_env({"DATA_PLANE_REPO": "acme/odoo"})
    orch = build_orchestrator(
        config, fake_client, decision=RolloutDecision.ACT
    )
    assert orch.shadow is False


def test_build_orchestrator_shadow_decision_keeps_shadow_true(fake_client):
    config = AgentConfig.from_env({"DATA_PLANE_REPO": "acme/odoo"})
    orch = build_orchestrator(
        config, fake_client, decision=RolloutDecision.SHADOW
    )
    assert orch.shadow is True


# -- build_github --------------------------------------------------------------
def test_build_github_act_returns_a_real_client():
    github = build_github("acme/odoo", RolloutDecision.ACT)
    assert isinstance(github, GhCliClient)
    assert not isinstance(github, ShadowGitHubClient)


def test_build_github_shadow_wraps_the_client_in_a_shadow():
    github = build_github("acme/odoo", RolloutDecision.SHADOW)
    assert isinstance(github, ShadowGitHubClient)


# -- run -----------------------------------------------------------------------
def test_run_with_no_event_does_nothing_and_exits_zero():
    log = _log()
    assert run({}, log=log) == 0
    assert "no-event" in _events(log)


def test_run_skips_when_the_kill_switch_is_off(tmp_path):
    """AGENTS_ENABLED=false -> the rollout gate returns SKIP; run exits 0 having
    built nothing (no DATA_PLANE_REPO needed to reach the SKIP)."""
    env = _event_env(
        tmp_path,
        "push",
        {"ref": "refs/heads/agent/spec-1"},
        AGENTS_ENABLED="false",
    )
    log = _log()
    assert run(env, log=log) == 0
    assert "skipped" in _events(log)


def test_run_routes_an_ignored_webhook_and_exits_zero(tmp_path):
    """A webhook the agent does not act on (a `star`) builds the object graph,
    routes through handle_webhook to None, and exits 0 — the offline wiring
    proof: composition root + Rollout + adapter, no OpenCode call."""
    env = _event_env(
        tmp_path,
        "star",
        {"action": "created"},
        DATA_PLANE_REPO="acme/odoo",
    )
    log = _log()
    assert run(env, log=log) == 0
    events = _events(log)
    assert "rollout-decision" in events
    assert "outcome" in events


def test_run_aborts_with_exit_one_when_data_plane_repo_is_missing(tmp_path):
    """A run that would ACT but has no DATA_PLANE_REPO is a misconfiguration —
    run exits non-zero rather than building a half-wired client."""
    env = _event_env(
        tmp_path,
        "push",
        {"ref": "refs/heads/agent/spec-1"},
        ROLLOUT_STAGE="default_on",
    )
    log = _log()
    assert run(env, log=log) == 1
    assert "misconfigured" in _events(log)


# -- build_notifier ------------------------------------------------------------
def test_build_notifier_act_with_url_returns_slack():
    config = AgentConfig.from_env({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"})
    assert isinstance(build_notifier(config, RolloutDecision.ACT), SlackNotifier)


def test_build_notifier_act_without_url_returns_fake():
    """ACT without a webhook still yields a no-op notifier — the agent must
    not crash because Slack isn't configured."""
    config = AgentConfig.from_env({})
    assert isinstance(build_notifier(config, RolloutDecision.ACT), FakeNotifier)


def test_build_notifier_shadow_returns_fake_even_with_url():
    """SHADOW suppresses every world-facing side effect, Slack included —
    even when a webhook URL is configured."""
    config = AgentConfig.from_env({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"})
    assert isinstance(build_notifier(config, RolloutDecision.SHADOW), FakeNotifier)


# -- notify_outcome ------------------------------------------------------------
def _escalated_flow_result() -> FlowResult:
    planning = PlanningResult(
        status="escalated", feature="widget", findings=["CRITICAL: contradiction"]
    )
    return FlowResult(status="escalated", stage="planning", planning=planning)


def test_notify_outcome_pings_slack_on_an_escalated_flow_result():
    fake = FakeNotifier()
    notify_outcome(fake, _escalated_flow_result(), pr=42)
    assert len(fake.sent) == 1
    channel, message, severity = fake.sent[0]
    assert "42" in message
    assert "planning" in message
    assert severity == "page"


def test_notify_outcome_pings_slack_on_an_escalated_iteration_result():
    fake = FakeNotifier()
    result = IterationResult(
        intent=CommentIntent.CHANGE_REQUEST, status="escalated", comment="…"
    )
    notify_outcome(fake, result, pr=17)
    assert len(fake.sent) == 1
    message = fake.sent[0][1]
    assert "17" in message
    assert "change_request" in message


def test_notify_outcome_is_a_noop_for_a_clean_implemented_outcome():
    fake = FakeNotifier()
    planning = PlanningResult(status="ready_to_implement", feature="x")
    clean = FlowResult(status="implemented", stage="implement", planning=planning)
    notify_outcome(fake, clean, pr=42)
    assert fake.sent == []


def test_notify_outcome_is_a_noop_when_result_is_none():
    fake = FakeNotifier()
    notify_outcome(fake, None, pr=42)
    assert fake.sent == []


def test_notify_outcome_is_a_noop_when_pr_is_unknown():
    """A push-event escalation has no PR number — skip Slack rather than
    page on-call with an unlinkable alert."""
    fake = FakeNotifier()
    notify_outcome(fake, _escalated_flow_result(), pr=None)
    assert fake.sent == []


def test_main_delegates_to_run(monkeypatch):
    import os

    captured: dict = {}

    def fake_run(env):
        captured["env"] = env
        return 0

    monkeypatch.setattr("agents.implementation.app.run", fake_run)
    assert main() == 0
    assert captured["env"] is os.environ
