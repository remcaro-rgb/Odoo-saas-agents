"""Unit tests for the deterministic Odoo rule checks (Phase C, §4.3)."""

from agents.implementation.odoo_rules import (
    check_manifest,
    check_model_access,
    check_orm_antipatterns,
    check_view_xml,
)

GOOD_MANIFEST = """{
    'name': 'Widget Counter',
    'version': '19.0.1.0.0',
    'depends': ['base', 'web'],
    'data': ['security/ir.model.access.csv', 'views/widget_views.xml'],
    'license': 'LGPL-3',
}"""


# -- manifest -----------------------------------------------------------------
def test_good_manifest_has_no_findings():
    assert check_manifest(GOOD_MANIFEST) == []


def test_manifest_missing_required_keys_is_flagged():
    findings = check_manifest("{'name': 'X', 'version': '1.0'}")
    messages = " ".join(f.message for f in findings)
    assert "depends" in messages
    assert "license" in messages


def test_unparseable_manifest_is_flagged():
    findings = check_manifest("{'name': oops not python}")
    assert findings
    assert findings[0].severity == "error"


# -- model access (ACL) -------------------------------------------------------
def test_model_without_acl_line_is_flagged():
    files = {
        "models/widget.py": "class Widget(models.Model):\n    _name = 'widget.counter'\n",
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,"
        "perm_read,perm_write,perm_create,perm_unlink\n",
    }
    findings = check_model_access(files)
    assert any("widget.counter" in f.message for f in findings)


def test_model_with_acl_line_passes():
    files = {
        "models/widget.py": "class Widget(models.Model):\n    _name = 'widget.counter'\n",
        "security/ir.model.access.csv": "id,name,model_id:id,group_id:id,"
        "perm_read,perm_write,perm_create,perm_unlink\n"
        "access_widget,access widget,model_widget_counter,base.group_user,1,1,1,1\n",
    }
    assert check_model_access(files) == []


# -- view XML -----------------------------------------------------------------
def test_malformed_view_xml_is_flagged():
    findings = check_view_xml("<odoo><record><field></record></odoo>")
    assert findings
    assert findings[0].rule == "view.malformed_xml"


def test_wellformed_view_xml_passes():
    assert check_view_xml("<odoo><record id='v'><field name='x'/></record></odoo>") == []


# -- ORM anti-patterns --------------------------------------------------------
def test_fstring_sql_is_flagged():
    code = 'def f(self):\n    self.env.cr.execute(f"SELECT * FROM t WHERE id={x}")\n'
    findings = check_orm_antipatterns(code)
    assert any(f.rule == "orm.raw_sql_formatting" for f in findings)


def test_format_string_sql_is_flagged():
    code = 'def f(self):\n    self.env.cr.execute("SELECT {}".format(tbl))\n'
    findings = check_orm_antipatterns(code)
    assert any(f.rule == "orm.raw_sql_formatting" for f in findings)


def test_parameterized_sql_passes():
    code = 'def f(self):\n    self.env.cr.execute("SELECT * FROM t WHERE id=%s", (x,))\n'
    assert check_orm_antipatterns(code) == []


def test_mutable_default_argument_is_flagged():
    findings = check_orm_antipatterns("def f(self, items=[]):\n    return items\n")
    assert any(f.rule == "orm.mutable_default" for f in findings)
