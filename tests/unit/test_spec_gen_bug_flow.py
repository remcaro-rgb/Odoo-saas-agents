"""Unit tests for the Tier 4 bug intake path through the Orchestrator."""

from __future__ import annotations

from agents.spec_generator.core import Orchestrator
from agents.spec_generator.events import Event, EventType
from agents.spec_generator.repro import (
    FakeAgentlabClient,
    ReproOutcome,
    ReproResult,
)


def _bug_event(**kw) -> Event:
    base = dict(
        type=EventType.ISSUE_OPENED,
        issue=42,
        title="PDF export broken",
        body=(
            "Steps:\n"
            "1. Click Print\n"
            "2. Choose PDF\n\n"
            "Expected: PDF downloads.\n"
            "Actual: 500 page."
        ),
        actor="alice",
        kind_hint="bug",
    )
    base.update(kw)
    return Event(**base)


def test_bug_without_agentlab_client_falls_back_to_human_triage(fake_client):
    """No agentlab client wired -> pre-Tier-4 posture."""
    orch = Orchestrator(oc_client=fake_client)  # default: no agentlab
    result = orch.draft_spec(_bug_event())
    assert result.status == "escalated"
    label_names = [name for _, name in result.labels]
    assert "needs-human" in label_names


def test_bug_with_confirmed_repro_drafts_fix_brief(fake_client):
    agentlab = FakeAgentlabClient(
        outcome_map={
            42: ReproResult(
                outcome=ReproOutcome.REPRO_CONFIRMED,
                summary="500 on PDF export",
                logs="KeyError: 'invoice_lines'",
                screenshots=("https://shots/1.png",),
            )
        }
    )
    orch = Orchestrator(oc_client=fake_client, agentlab=agentlab)
    result = orch.draft_spec(_bug_event())
    assert result.status == "drafted"
    assert result.drafted is not None
    assert result.drafted.path.endswith("-fix.md")
    assert "fix-brief" in result.drafted.body.lower()
    assert "invoice_lines" in result.drafted.body  # log tail folded in
    # /speckit.specify must NOT be called for a bug flow — fix-brief is a
    # deterministic template fill-in, not an LLM draft.
    commands = [c["command"] for c in fake_client.commands]
    assert "speckit.specify" not in commands


def test_bug_with_needs_repro_info_posts_targeted_questions(fake_client):
    agentlab = FakeAgentlabClient()
    orch = Orchestrator(oc_client=fake_client, agentlab=agentlab)
    result = orch.draft_spec(_bug_event(body="halp it broke"))
    assert result.status == "escalated"
    label_names = [name for _, name in result.labels]
    assert "needs-repro-info" in label_names
    # Comment contains the question prompts.
    body = "\n".join(b for _, b in result.comments)
    assert "exact steps" in body.lower() or "what happened" in body.lower()


def test_bug_with_needs_fixture_routes_to_security_leads(fake_client):
    agentlab = FakeAgentlabClient()
    orch = Orchestrator(oc_client=fake_client, agentlab=agentlab)
    result = orch.draft_spec(
        _bug_event(body="see our prod database tenant id 17")
    )
    assert result.status == "escalated"
    label_names = [name for _, name in result.labels]
    assert "needs-security-triage" in label_names


def test_bug_when_agentlab_unavailable_escalates_human(fake_client):
    agentlab = FakeAgentlabClient(
        outcome_map={
            42: ReproResult(
                outcome=ReproOutcome.AGENTLAB_UNAVAILABLE,
                summary="shim 504",
            )
        }
    )
    orch = Orchestrator(oc_client=fake_client, agentlab=agentlab)
    result = orch.draft_spec(_bug_event())
    assert result.status == "escalated"
    label_names = [name for _, name in result.labels]
    assert "needs-human" in label_names
