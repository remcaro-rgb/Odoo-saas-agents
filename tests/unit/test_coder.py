"""Unit tests for the coder.py Odoo specialization layer (Phase C, §4.3)."""

from agents.implementation.coder import (
    Coder,
    ImplementResult,
    correct,
    inject_context,
    scaffold,
    validate_odoo,
)
from agents.implementation.gate1 import FakeCheckRunner, Gate1
from agents.implementation.odoo_rules import Finding, check_manifest
from agents.implementation.speckit_driver import SpecKitDriver
from agents.implementation.workspace import InMemoryWorkspace

GOOD_MANIFEST = """{
    'name': 'Widget Counter',
    'version': '19.0.1.0.0',
    'depends': ['base', 'web'],
    'data': ['security/ir.model.access.csv'],
    'license': 'LGPL-3',
}"""

ACL_HEADER = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
)


def _clean_addon() -> dict[str, str]:
    return {
        "custom-addons/widget/__manifest__.py": GOOD_MANIFEST,
        "custom-addons/widget/models/widget.py": (
            "class Widget(models.Model):\n    _name = 'widget.counter'\n"
        ),
        "custom-addons/widget/security/ir.model.access.csv": (
            ACL_HEADER
            + "access_widget,a,model_widget_counter,base.group_user,1,1,1,1\n"
        ),
    }


def _dirty_addon() -> dict[str, str]:
    # the model has no matching ir.model.access.csv line -> a persistent error
    files = _clean_addon()
    files["custom-addons/widget/security/ir.model.access.csv"] = ACL_HEADER
    return files


# -- scaffold -----------------------------------------------------------------
def test_scaffold_emits_a_manifest_that_passes_the_manifest_check():
    files = scaffold("widget_counter")
    manifest = files["custom-addons/widget_counter/__manifest__.py"]
    assert check_manifest(manifest) == []


def test_scaffold_emits_an_acl_line_per_model():
    files = scaffold("widget_counter", models=["widget.counter"])
    acl = files["custom-addons/widget_counter/security/ir.model.access.csv"]
    assert "model_widget_counter" in acl


def test_scaffold_emits_init_files():
    files = scaffold("widget_counter")
    assert "custom-addons/widget_counter/__init__.py" in files
    assert "custom-addons/widget_counter/models/__init__.py" in files


# -- validate_odoo ------------------------------------------------------------
def test_validate_odoo_passes_a_clean_addon():
    assert validate_odoo(_clean_addon()) == []


def test_validate_odoo_flags_a_missing_acl_entry():
    findings = validate_odoo(_dirty_addon())
    assert any(f.rule == "security.missing_acl" for f in findings)


# -- correct ------------------------------------------------------------------
def test_correct_builds_a_precise_reprompt_from_findings():
    findings = [
        Finding(
            "security.missing_acl",
            "model 'widget.counter' has no ir.model.access.csv entry",
        )
    ]
    prompt = correct(findings)
    assert "widget.counter" in prompt
    assert "ir.model.access.csv" in prompt


# -- inject_context -----------------------------------------------------------
def test_inject_context_summarizes_the_addon():
    ws = InMemoryWorkspace(_clean_addon())
    ctx = inject_context(ws, "custom-addons/widget/")
    assert "widget.counter" in ctx
    assert "__manifest__.py" in ctx


# -- the Coder loop -----------------------------------------------------------
def test_implement_clean_addon_succeeds_without_corrective_retries(fake_client):
    ws = InMemoryWorkspace(_clean_addon())
    result = Coder(SpecKitDriver(fake_client), ws).implement(
        "sess-1", "custom-addons/widget/"
    )
    assert isinstance(result, ImplementResult)
    assert result.status == "implemented"
    implements = [c for c in fake_client.commands if c["command"] == "speckit.implement"]
    assert len(implements) == 1            # just the initial pass
    assert implements[0]["model"] is None  # routine work -> default model


def test_implement_persistently_invalid_addon_escalates_after_the_cap(fake_client):
    ws = InMemoryWorkspace(_dirty_addon())
    result = Coder(SpecKitDriver(fake_client), ws, max_retries=3).implement(
        "sess-1", "custom-addons/widget/"
    )
    assert result.status == "escalated"
    assert ws.escalations[-1].reason == "needs-human"


def test_implement_routes_corrective_retries_to_the_frontier_model(fake_client):
    ws = InMemoryWorkspace(_dirty_addon())
    Coder(
        SpecKitDriver(fake_client),
        ws,
        max_retries=3,
        frontier_model="anthropic/claude-sonnet-4-6",
    ).implement("sess-1", "custom-addons/widget/")
    implements = [c for c in fake_client.commands if c["command"] == "speckit.implement"]
    assert len(implements) == 4  # 1 initial + 3 corrective
    assert implements[0]["model"] is None
    assert all(c["model"] == "anthropic/claude-sonnet-4-6" for c in implements[1:])


def test_implement_primes_the_session_with_odoo_context(fake_client):
    """implement() injects the addon's Odoo context into the session before coding."""
    ws = InMemoryWorkspace(_clean_addon())
    Coder(SpecKitDriver(fake_client), ws).implement("sess-1", "custom-addons/widget/")
    assert fake_client.messages, "expected an Odoo-context priming message"
    session_id, text = fake_client.messages[0]
    assert session_id == "sess-1"
    assert "widget.counter" in text


def test_implement_scaffolds_boilerplate_for_a_brand_new_addon(fake_client):
    """A brand-new (empty) addon prefix gets correct-by-construction boilerplate."""
    ws = InMemoryWorkspace()
    result = Coder(SpecKitDriver(fake_client), ws).implement(
        "sess-1", "custom-addons/newmod/"
    )
    assert ws.exists("custom-addons/newmod/__manifest__.py")
    assert result.status == "implemented"


# -- session-diff sync (Tier-4 Action ⇄ container workspace sync) -------------
def test_implement_syncs_opencode_session_diff_before_validation(fake_client):
    """After each `/speckit.implement`, the coder pulls OpenCode's session
    diff into the local workspace so validate_odoo / Gate-1 sees what the
    agent actually wrote (runbook §7 Tier-4 fix).

    Without the sync, an addon that's incomplete on the Action-side would
    stay incomplete from the validator's view (the agent's writes are
    stranded in the OpenCode container). With the sync, the diff lands in
    `ws.files` before validation and a clean run completes at attempt 0.
    """
    # Start incomplete: manifest is there, but no model and no ACL yet.
    ws = InMemoryWorkspace(
        {
            "custom-addons/widget/__manifest__.py": GOOD_MANIFEST,
            "custom-addons/widget/__init__.py": "from . import models\n",
        }
    )
    # The "agent" returns a diff that adds the missing files.
    fake_client.set_diff(
        [
            {
                "file": "custom-addons/widget/models/widget.py",
                "status": "added",
                "content": (
                    "class Widget(models.Model):\n"
                    "    _name = 'widget.counter'\n"
                ),
            },
            {
                "file": "custom-addons/widget/security/ir.model.access.csv",
                "status": "added",
                "content": (
                    ACL_HEADER
                    + "access_widget,a,model_widget_counter,"
                    "base.group_user,1,1,1,1\n"
                ),
            },
        ]
    )
    result = Coder(SpecKitDriver(fake_client), ws).implement(
        "ses_x", "custom-addons/widget/"
    )
    # The diff was synced into the workspace before validate_odoo ran.
    assert "custom-addons/widget/models/widget.py" in ws.files
    assert "custom-addons/widget/security/ir.model.access.csv" in ws.files
    # And the addon is now clean -> implemented at attempt 0.
    assert result.status == "implemented"


# -- Gate 1 wiring ------------------------------------------------------------
def test_implement_runs_gate1_after_the_odoo_rules_pass(fake_client):
    """When a Gate1 is configured, it runs once the Odoo rules are clean."""
    ws = InMemoryWorkspace(_clean_addon())
    runner = FakeCheckRunner()
    result = Coder(SpecKitDriver(fake_client), ws, gate1=Gate1(runner)).implement(
        "sess-1", "custom-addons/widget/"
    )
    assert result.status == "implemented"
    assert runner.commands  # Gate 1 actually ran its checks
    assert result.gate is not None and result.gate.passed


def test_implement_escalates_when_gate1_keeps_failing(fake_client):
    """A clean addon (Odoo rules pass) whose Gate 1 keeps failing escalates."""
    ws = InMemoryWorkspace(_clean_addon())
    runner = FakeCheckRunner()
    runner.set_result("ruff check", 1, "E501 line too long")
    result = Coder(
        SpecKitDriver(fake_client), ws, max_retries=2, gate1=Gate1(runner)
    ).implement("sess-1", "custom-addons/widget/")
    assert result.status == "escalated"
    assert ws.escalations[-1].reason == "needs-human"
    assert "Gate 1" in ws.escalations[-1].details


def test_implement_reprompts_the_agent_with_the_gate1_failure(fake_client):
    """A Gate-1 failure feeds a corrective /implement re-prompt carrying its logs."""
    ws = InMemoryWorkspace(_clean_addon())
    runner = FakeCheckRunner()
    runner.set_result("ruff check", 1, "UNIQUE_LINT_MARKER")
    Coder(
        SpecKitDriver(fake_client), ws, max_retries=1, gate1=Gate1(runner)
    ).implement("sess-1", "custom-addons/widget/")
    implements = [
        c for c in fake_client.commands if c["command"] == "speckit.implement"
    ]
    assert any("UNIQUE_LINT_MARKER" in c["arguments"] for c in implements)
