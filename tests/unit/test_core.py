"""Unit tests for the orchestrator core (Phase B) — routing + the planning flow."""

from agents.implementation.classifier import CommentIntent
from agents.implementation.core import (
    Orchestrator,
    SessionRecord,
    addon_prefix_from_spec,
    feature_name,
    load_session,
    route,
    save_session,
)
from agents.implementation.events import Event, EventType
from agents.implementation.gate1 import FakeCheckRunner, Gate1
from agents.implementation.speckit_driver import SpecKitDriver
from agents.implementation.workspace import InMemoryWorkspace

DESIGN_SPEC = "# Widget — Design Spec\n\n## 1. Goal\nAdd a widget counter.\n"

# A clean /speckit.analyze report. Planning proceeds only when the spec/plan/tasks
# set coheres, so happy-path tests must supply this — an empty report escalates.
CLEAN_ANALYZE = {
    "parts": [{"type": "text", "text": "Analysis complete. No inconsistencies found."}]
}


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


def test_fix_brief_planning_preserves_spec_text_for_the_coder(fake_client):
    """The fast-path skips /plan + /tasks, so /implement has no tasks.md or
    plan.md to read. PlanningResult must carry the spec body forward so the
    coder can pass it as $ARGUMENTS to /speckit.implement (Tier-5: fold the
    fix-brief body into the implement directive)."""
    body = "## 1. Symptom\nFoo is broken.\n\n## 5. Proposed fix\nDo bar.\n"
    ws = InMemoryWorkspace({"docs/superpowers/specs/login-fix.md": body})
    result = Orchestrator(ws, SpecKitDriver(fake_client)).run_planning(_fix_event())
    assert result.fast_path is True
    assert result.spec_text == body


def test_design_spec_runs_plan_then_tasks_then_analyze(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
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
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
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


# -- the full implement flow: B (planning) + C (coder) wired together ----------
_ACL_HEADER = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
)


def _clean_addon_files() -> dict[str, str]:
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


def test_implement_runs_planning_then_hands_off_to_the_coder(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    result = Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _design_event(), "custom-addons/widget/"
    )
    assert result.status == "implemented"
    commands = [c["command"] for c in fake_client.commands]
    assert "speckit.plan" in commands
    assert "speckit.implement" in commands


def test_implement_uses_a_single_session_for_planning_and_coding(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _design_event(), "custom-addons/widget/"
    )
    assert len(fake_client.created_sessions) == 1


def test_implement_does_not_run_the_coder_when_planning_escalates(fake_client):
    fake_client.set_command_result(
        "speckit.analyze",
        {"parts": [{"type": "text", "text": "CRITICAL: spec contradicts the model"}]},
    )
    ws = InMemoryWorkspace({"docs/superpowers/specs/widget-design.md": DESIGN_SPEC})
    result = Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _design_event(), "custom-addons/widget/"
    )
    assert result.status == "escalated"
    assert "speckit.implement" not in [c["command"] for c in fake_client.commands]


def test_implement_fix_brief_skips_planning_but_still_codes(fake_client):
    ws = InMemoryWorkspace(
        {
            "docs/superpowers/specs/login-fix.md": "## 1. Goal\nfix it",
            **_clean_addon_files(),
        }
    )
    result = Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _fix_event(), "custom-addons/widget/"
    )
    assert result.status == "implemented"
    commands = [c["command"] for c in fake_client.commands]
    assert "speckit.plan" not in commands
    assert "speckit.implement" in commands
    assert len(fake_client.created_sessions) == 1


# -- session persistence (Phase D) ---------------------------------------------
def test_save_then_load_session_round_trips():
    ws = InMemoryWorkspace()
    record = SessionRecord("ses_abc123", "custom-addons/widget/")
    save_session(ws, "spec-1500", record)
    assert load_session(ws, "spec-1500") == record


def test_load_session_returns_none_when_none_saved():
    assert load_session(InMemoryWorkspace(), "spec-1500") is None


def test_implement_persists_the_session_for_a_later_reporter_comment(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _design_event(), "custom-addons/widget/"
    )
    record = load_session(ws, "spec-1500")
    assert record is not None
    assert record.session_id
    assert record.addon_prefix == "custom-addons/widget/"


# -- addon-prefix derivation (Phase D: intent_confirmed wiring) ----------------
def test_addon_prefix_from_spec_uses_the_addon_the_spec_names():
    spec = "Scope of work: a new addon at `custom-addons/equipment_checkout`."
    assert (
        addon_prefix_from_spec(spec, "spec-1500")
        == "custom-addons/equipment_checkout/"
    )


def test_addon_prefix_from_spec_falls_back_to_the_feature_when_unnamed():
    assert (
        addon_prefix_from_spec(DESIGN_SPEC, "spec-1500") == "custom-addons/spec-1500/"
    )


def test_addon_prefix_from_spec_takes_the_first_addon_mentioned():
    spec = "touches custom-addons/alpha, and later custom-addons/beta too"
    assert addon_prefix_from_spec(spec, "spec-1") == "custom-addons/alpha/"


def test_implement_derives_the_addon_prefix_from_the_spec_when_omitted(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    spec = DESIGN_SPEC + "\nThe addon lives at custom-addons/widget.\n"
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": spec, **_clean_addon_files()}
    )
    result = Orchestrator(ws, SpecKitDriver(fake_client)).implement(_design_event())
    assert result.status == "implemented"
    record = load_session(ws, "spec-1500")
    assert record is not None
    assert record.addon_prefix == "custom-addons/widget/"


# -- reporter iteration (Phase D) ----------------------------------------------
def _comment_event(comment: str) -> Event:
    return Event(
        type=EventType.ISSUE_COMMENT,
        branch="agent/spec-1500",
        pr=42,
        comment=comment,
    )


def _addon_files_missing_acl() -> dict[str, str]:
    """A clean addon, except the model has no ir.model.access.csv entry."""
    files = _clean_addon_files()
    files["custom-addons/widget/security/ir.model.access.csv"] = _ACL_HEADER
    return files


def test_reporter_iteration_change_request_runs_the_coder_loop(fake_client):
    ws = InMemoryWorkspace(_clean_addon_files())
    save_session(ws, "spec-1500", SessionRecord("ses_saved", "custom-addons/widget/"))
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("Please rename the field to internal_note")
    )
    assert result.intent is CommentIntent.CHANGE_REQUEST
    assert result.status == "iterated"
    assert result.session_id == "ses_saved"
    implements = [c for c in fake_client.commands if c["command"] == "speckit.implement"]
    assert implements and implements[0]["session_id"] == "ses_saved"
    assert any("internal_note" in text for _, text in fake_client.messages)
    assert result.comment


def test_reporter_iteration_change_request_escalates_when_validation_fails(fake_client):
    ws = InMemoryWorkspace(_addon_files_missing_acl())
    save_session(ws, "spec-1500", SessionRecord("ses_saved", "custom-addons/widget/"))
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("Please rename the field")
    )
    assert result.status == "escalated"
    assert ws.escalations


def test_reporter_iteration_change_request_without_a_session_escalates(fake_client):
    ws = InMemoryWorkspace()  # no saved session
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("Please rename the field")
    )
    assert result.status == "escalated"
    assert ws.escalations
    assert fake_client.commands == []


def test_reporter_iteration_question_escalates_to_a_human(fake_client):
    ws = InMemoryWorkspace()
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("Why is the note on a separate tab?")
    )
    assert result.intent is CommentIntent.QUESTION
    assert result.status == "escalated"
    assert ws.escalations
    assert fake_client.commands == []


def test_reporter_iteration_approval_is_acknowledged(fake_client):
    ws = InMemoryWorkspace()
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("LGTM, ship it")
    )
    assert result.status == "acknowledged"
    assert ws.escalations == []
    assert fake_client.commands == []


def test_reporter_iteration_noise_is_ignored(fake_client):
    ws = InMemoryWorkspace()
    result = Orchestrator(ws, SpecKitDriver(fake_client)).reporter_iteration(
        _comment_event("thanks, appreciate it!")
    )
    assert result.status == "ignored"
    assert ws.escalations == []
    assert fake_client.commands == []


# -- workspace provisioning wiring (Phase D) -----------------------------------
def test_implement_provisions_the_workspace_when_a_repo_is_configured(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    Orchestrator(
        ws, SpecKitDriver(fake_client), repo="GoliattCo/odoo-custom"
    ).implement(_design_event(), "custom-addons/widget/")
    assert any(
        "GoliattCo/odoo-custom" in text and "agent/spec-1500" in text
        for _, text in fake_client.messages
    )


def test_implement_does_not_provision_without_a_configured_repo(fake_client):
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    Orchestrator(ws, SpecKitDriver(fake_client)).implement(
        _design_event(), "custom-addons/widget/"
    )
    assert not any("GoliattCo" in text for _, text in fake_client.messages)


def test_reporter_iteration_provisions_the_workspace_before_iterating(fake_client):
    ws = InMemoryWorkspace(_clean_addon_files())
    save_session(ws, "spec-1500", SessionRecord("ses_saved", "custom-addons/widget/"))
    Orchestrator(
        ws, SpecKitDriver(fake_client), repo="GoliattCo/odoo-custom"
    ).reporter_iteration(_comment_event("Please rename the field"))
    assert any("GoliattCo/odoo-custom" in text for _, text in fake_client.messages)


# -- Gate 1 wiring (Phase C) ---------------------------------------------------
def test_implement_runs_gate1_when_the_orchestrator_has_one(fake_client):
    """A Gate1 configured on the Orchestrator is threaded into the coder loop —
    a failing Gate 1 escalates the implement flow."""
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    runner = FakeCheckRunner()
    runner.set_result("ruff check", 1, "lint failure")
    result = Orchestrator(
        ws, SpecKitDriver(fake_client), gate1=Gate1(runner)
    ).implement(_design_event(), "custom-addons/widget/")
    assert result.status == "escalated"
    assert result.stage == "implement"


def test_implement_succeeds_when_the_orchestrators_gate1_passes(fake_client):
    """A passing Gate-1 threaded from the Orchestrator lets implement complete."""
    fake_client.set_command_result("speckit.analyze", CLEAN_ANALYZE)
    ws = InMemoryWorkspace(
        {"docs/superpowers/specs/widget-design.md": DESIGN_SPEC, **_clean_addon_files()}
    )
    result = Orchestrator(
        ws, SpecKitDriver(fake_client), gate1=Gate1(FakeCheckRunner())
    ).implement(_design_event(), "custom-addons/widget/")
    assert result.status == "implemented"
    assert result.implement is not None
    assert result.implement.gate is not None and result.implement.gate.passed
