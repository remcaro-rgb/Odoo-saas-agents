"""Unit tests for the project-spec <-> Spec-Kit mapper (Phase B, decision 1.7)."""

from agents.implementation.events import SpecKind
from agents.implementation.spec_mapper import (
    detect_spec_kind,
    parse_sections,
    to_speckit,
)

DESIGN_SPEC = """# Widget Counter — Design Spec

**Status:** Draft

## 1. Goal
Add a per-tenant widget counter to the dashboard.

## 2. Non-goals
- No historical charts.

## 7. Test plan
- Counter increments on widget create.
"""


def test_parse_sections_splits_on_headings():
    sections = parse_sections(DESIGN_SPEC)
    assert "1. Goal" in sections
    assert "Add a per-tenant widget counter" in sections["1. Goal"]


def test_parse_sections_captures_the_h1_title_as_a_section():
    sections = parse_sections(DESIGN_SPEC)
    assert any("Widget Counter" in heading for heading in sections)


def test_parse_sections_keeps_section_bodies_separate():
    sections = parse_sections(DESIGN_SPEC)
    assert "historical charts" in sections["2. Non-goals"]
    assert "historical charts" not in sections["1. Goal"]


def test_to_speckit_emits_the_speckit_mandatory_sections():
    out = to_speckit(DESIGN_SPEC)
    assert "## User Scenarios" in out
    assert "## Requirements" in out
    assert "## Success Criteria" in out


def test_to_speckit_carries_the_project_goal_through():
    out = to_speckit(DESIGN_SPEC)
    assert "widget counter" in out.lower()


def test_detect_spec_kind_design_by_filename():
    assert detect_spec_kind("docs/specs/2026-05-20-widget-design.md", "") is SpecKind.DESIGN


def test_detect_spec_kind_fix_by_filename():
    assert detect_spec_kind("docs/specs/2026-05-20-login-fix.md", "") is SpecKind.FIX


def test_detect_spec_kind_falls_back_to_design():
    assert detect_spec_kind("spec.md", "## 1. Goal\nDo a thing.") is SpecKind.DESIGN


def test_find_does_not_match_a_needle_inside_a_word():
    """The 'test' needle must not pick up an unrelated section like 'Latest'."""
    spec = (
        "# T\n\n"
        "## Latest changes\nUnrelated release notes.\n\n"
        "## 1. Goal\nThe real goal.\n"
    )
    out = to_speckit(spec)
    assert "Unrelated release notes" not in out
