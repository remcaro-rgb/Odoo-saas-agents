"""Unit tests for the IntakeBuilder."""

from __future__ import annotations

import pytest

from agents.spec_generator.events import Event, EventType
from agents.spec_generator.intake import IntakeBuilder


def _evt(**kw: object) -> Event:
    base = {
        "type": EventType.ISSUE_OPENED,
        "issue": 12,
        "title": "Add CSV export",
        "body": "Please add a download-as-CSV button.",
        "actor": "alice",
    }
    base.update(kw)
    return Event(**base)  # type: ignore[arg-type]


def test_build_returns_intake_with_basics():
    intake = IntakeBuilder().build(_evt())
    assert intake.issue == 12
    assert intake.title == "Add CSV export"
    assert intake.reporter == "alice"
    assert intake.language == "en"
    assert intake.attachments == ()
    assert intake.labels == ()


def test_build_propagates_kind_hint_from_event_and_labels_from_caller():
    intake = IntakeBuilder().build(
        _evt(kind_hint="feature-request"), labels=["feature-request", "p1"]
    )
    assert intake.kind_hint == "feature-request"
    assert intake.labels == ("feature-request", "p1")


def test_non_ascii_body_flags_other_language():
    intake = IntakeBuilder().build(
        _evt(body="Necesitamos un botón para exportar a CSV.")
    )
    assert intake.language == "other"


def test_markdown_image_attachments_are_extracted():
    body = (
        "Reproducer screenshot:\n"
        "![bug](https://user-images.githubusercontent.com/1/2.png)\n"
        "and a separate video: https://example.com/video.mp4"
    )
    intake = IntakeBuilder().build(_evt(body=body))
    assert (
        "https://user-images.githubusercontent.com/1/2.png" in intake.attachments
    )
    assert "https://example.com/video.mp4" in intake.attachments


def test_empty_event_issue_raises():
    with pytest.raises(ValueError, match="issue number"):
        IntakeBuilder().build(_evt(issue=None))


def test_title_is_trimmed():
    intake = IntakeBuilder().build(_evt(title="  Add CSV export  "))
    assert intake.title == "Add CSV export"
