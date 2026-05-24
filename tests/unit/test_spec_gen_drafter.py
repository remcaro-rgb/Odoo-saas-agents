"""Unit tests for Drafter — intake -> DraftedSpec."""

from __future__ import annotations

from agents.spec_generator.drafter import (
    Drafter,
    feature_branch,
    spec_path,
)
from agents.spec_generator.intake import Intake
from agents.spec_generator.speckit_driver import SpecKitFrontDriver


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.last_arguments: str | None = None

    def run_command(self, session_id, command, arguments="", *, model=None):
        self.last_arguments = arguments
        return {"parts": [{"type": "text", "text": self.text}]}


def _intake(**kw):
    base = dict(
        issue=12,
        title="Add CSV export",
        body="Please add a CSV download.",
        reporter="alice",
        language="en",
    )
    base.update(kw)
    return Intake(**base)


def test_feature_branch_naming():
    assert feature_branch(7, "Add CSV export") == "agent/spec-0007-add-csv-export"


def test_spec_path_naming():
    assert (
        spec_path(7, "Add CSV export")
        == "docs/superpowers/specs/spec-0007-add-csv-export-design.md"
    )


def test_branch_naming_handles_empty_title():
    assert feature_branch(99, "") == "agent/spec-0099-spec"


def test_branch_naming_handles_pure_punctuation_title():
    assert feature_branch(99, "!!!") == "agent/spec-0099-spec"


def test_draft_design_spec_returns_full_drafted_spec():
    spec_text = "# CSV export\n- captured A\n- [NEEDS CLARIFICATION: which TZ?]"
    drafter = Drafter(SpecKitFrontDriver(_FakeClient(spec_text)))
    drafted = drafter.draft_design_spec(
        intake=_intake(), session_id="sess-1"
    )
    assert drafted.issue == 12
    assert drafted.branch == "agent/spec-0012-add-csv-export"
    assert drafted.path.endswith("spec-0012-add-csv-export-design.md")
    assert drafted.body == spec_text
    assert drafted.session_id == "sess-1"
    assert drafted.open_questions == ("which TZ?",)


def test_prompt_prepends_provenance_and_includes_attachments():
    client = _FakeClient(text="ok")
    drafter = Drafter(SpecKitFrontDriver(client))
    intake = _intake(attachments=("https://example.com/1.png",))
    drafter.draft_design_spec(intake=intake, session_id="s")
    assert client.last_arguments is not None
    assert "Issue #12" in client.last_arguments
    assert "alice" in client.last_arguments
    assert "https://example.com/1.png" in client.last_arguments


def test_captured_items_summary_includes_issue_and_attachment_count():
    client = _FakeClient(text="some draft")
    drafter = Drafter(SpecKitFrontDriver(client))
    intake = _intake(
        attachments=("a.png", "b.png"), kind_hint="feature-request"
    )
    drafted = drafter.draft_design_spec(intake=intake, session_id="s")
    joined = " | ".join(drafted.captured_items)
    assert "#12" in joined
    assert "2 attachment" in joined
    assert "feature-request" in joined


def test_empty_spec_text_propagates_empty_body():
    drafter = Drafter(SpecKitFrontDriver(_FakeClient(text="")))
    drafted = drafter.draft_design_spec(intake=_intake(), session_id="s")
    assert drafted.body == ""
    assert drafted.open_questions == ()
