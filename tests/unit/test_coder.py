"""Unit tests for the coder.py Odoo specialization layer (Phase C, §4.3)."""

from agents.implementation.coder import (
    Coder,
    ImplementResult,
    correct,
    inject_context,
    scaffold,
    validate_odoo,
)
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
