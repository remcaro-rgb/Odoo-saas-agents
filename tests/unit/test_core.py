"""Unit tests for the orchestrator core (Phase B) — routing + the planning flow."""

from agents.implementation.core import Orchestrator, feature_name, route
from agents.implementation.events import Event, EventType
from agents.implementation.speckit_driver import SpecKitDriver
from agents.implementation.workspace import InMemoryWorkspace

DESIGN_SPEC = "# Widget — Design Spec\n\n## 1. Goal\nAdd a widget counter.\n"


def _design_event() -> Event:
    return Event(
        type=EventType.INTENT_CONFIRMED,
        branch="agent/spec-1500",
        spec_path="docs/superpowers/specs/widget-design.md",
    )


def _fix_event() -> Event:
    return Event(
        type=EventType.INTENT_CONFIRMED,
        branch="agent/spec-1600",
        spec_path="docs/superpowers/specs/login-fix.md",
    )


def test_route_intent_confirmed_to_the_implement_flow():
    assert route(Event(type=EventType.INTENT_CONFIRMED)) == "implement"


def test_route_issue_comment_to_the_reporter_iteration_flow():
    assert route(Event(type=EventType.ISSUE_COMMENT)) == "reporter_iteration"


def test_feature_name_strips_the_branch_namespace():
    assert feature_name("agent/spec-1500") == "spec-1500"


def test_fix_brief_takes_the_fast_path_and_skips_plan_and_tasks(fake_client):
    ws = InMemoryWorkspace({"docs/superpowers/specs/login-fix.md": "## 1. Goal\nfix it"})
    result = Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_fix_event())
    assert result.fast_path is True
    assert result.status == "ready_to_implement"
    assert fake_client.commands == []          # no /plan or /tasks
    assert fake_client.created_sessions == []  # no session opened


def test_design_spec_runs_plan_then_tasks_then_analyze(fake_client):
    ws = InMemoryWorkspace({"docs/superpowers/specs/widget-design.md": DESIGN_SPEC})
    result = Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_design_event())
    issued = [c["command"] for c in fake_client.commands]
    assert issued == ["speckit.plan", "speckit.tasks", "speckit.analyze"]
    assert result.status == "ready_to_implement"


def test_design_spec_writes_the_mapped_speckit_spec(fake_client):
    ws = InMemoryWorkspace({"docs/superpowers/specs/widget-design.md": DESIGN_SPEC})
    Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_design_event())
    assert ws.exists("specs/spec-1500/spec.md")
    assert "## User Scenarios" in ws.read("specs/spec-1500/spec.md")


def test_incoherent_analyze_escalates_and_does_not_commit(fake_client):
    fake_client.set_command_result(
        "speckit.analyze",
        {"parts": [{"type": "text", "text": "CRITICAL: spec contradicts the data model"}]},
    )
    ws = InMemoryWorkspace({"docs/superpowers/specs/widget-design.md": DESIGN_SPEC})
    result = Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_design_event())
    assert result.status == "escalated"
    assert ws.escalations[-1].reason == "spec-refinement-needed"
    assert ws.commits == []
    assert result.findings


def test_coherent_design_spec_commits_the_planning_artifacts(fake_client):
    ws = InMemoryWorkspace(
        {
            "docs/superpowers/specs/widget-design.md": DESIGN_SPEC,
            "specs/spec-1500/plan.md": "the plan",   # as if OpenCode's /plan wrote it
            "specs/spec-1500/tasks.md": "the tasks",
        }
    )
    Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_design_event())
    assert ws.commits, "expected a planning commit"
    committed = ws.commits[-1].paths
    assert "specs/spec-1500/plan.md" in committed
    assert "specs/spec-1500/tasks.md" in committed
    assert ws.commits[-1].message.startswith("[impl-agent] plan:")
