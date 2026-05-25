"""Unit tests for the Spec Generator Orchestrator."""

from __future__ import annotations

import pytest

from agents.spec_generator.classifier import IntakeKind, KindResult
from agents.spec_generator.core import (
    LOW_CONFIDENCE_NOTICE_THRESHOLD,
    Orchestrator,
    SkipReason,
    feature_name,
)
from agents.spec_generator.events import Event, EventType


@pytest.fixture()
def opencode_with_specify_reply(fake_client):
    """Returns the shared fake_client preloaded with a /speckit.specify reply."""
    fake_client.set_command_result(
        "speckit.specify",
        {
            "parts": [
                {
                    "type": "text",
                    "text": (
                        "# CSV export\n"
                        "- capture line\n"
                        "- [NEEDS CLARIFICATION: which timezone?]\n"
                    ),
                }
            ]
        },
    )
    return fake_client


def _evt(**kw) -> Event:
    base = dict(
        type=EventType.ISSUE_OPENED,
        issue=42,
        title="Add CSV export",
        body="Please add a CSV download for sale orders.",
        actor="alice",
    )
    base.update(kw)
    return Event(**base)


def test_feature_name_extracts_slug():
    assert (
        feature_name("agent/spec-0042-add-csv-export") == "add-csv-export"
    )
    assert feature_name("main") == "main"


def test_drafts_a_feature_request_via_speckit_specify(opencode_with_specify_reply):
    orch = Orchestrator(oc_client=opencode_with_specify_reply)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "drafted"
    assert result.drafted is not None
    assert result.drafted.branch.startswith("agent/spec-0042-")
    # /speckit.specify was the one OpenCode command invoked.
    commands_seen = [c["command"] for c in opencode_with_specify_reply.commands]
    assert commands_seen == ["speckit.specify"]
    # The bot's summary comment and the two routing labels were recorded.
    assert any("issue" == target for target, _ in result.comments)
    label_names = [name for _, name in result.labels]
    assert "spec-drafted" in label_names
    assert "awaiting-reporter-confirm" in label_names


def test_sensitive_intake_does_not_call_opencode(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    result = orch.draft_spec(
        _evt(body="My password is hunter2 and AWS key is AKIAIOSFODNN7EXAMPLE.")
    )
    assert result.status == "escalated"
    assert result.skip_reason is SkipReason.SENSITIVE_CONTENT
    # The whole point of sensitive escalation: no LLM ever sees the body.
    assert fake_client.commands == []
    label_names = [name for _, name in result.labels]
    assert "needs-security-triage" in label_names


def test_bug_intake_without_agentlab_falls_back_to_human_triage(fake_client):
    """Tier 4 ships an AgentlabClient; without one the orchestrator routes
    bugs to human triage (the pre-Tier-4 posture)."""
    orch = Orchestrator(oc_client=fake_client)  # no agentlab
    result = orch.draft_spec(_evt(kind_hint="bug", body="It crashes."))
    assert result.status == "escalated"
    assert result.skip_reason is SkipReason.UNSUPPORTED_KIND
    assert fake_client.commands == []
    label_names = [name for _, name in result.labels]
    assert "needs-human" in label_names


def test_config_intake_routes_to_support(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    # Use a neutral title — the FEATURE regex matches "add" in the default
    # _evt() title, which would (correctly) route to the draft path instead.
    result = orch.draft_spec(
        _evt(
            title="SMTP question",
            body="How do I configure SMTP for this tenant?",
        )
    )
    assert result.status == "escalated"
    label_names = [name for _, name in result.labels]
    assert "needs-support" in label_names


def test_low_confidence_attaches_an_extra_notice(fake_client):
    # A neutral title + body with no routing signals forces the fallback
    # FEATURE path with confidence 0.3 (default _evt's title contains "Add"
    # which would match the FEATURE regex and bump confidence to 0.7).
    fake_client.set_command_result(
        "speckit.specify",
        {"parts": [{"type": "text", "text": "drafted body"}]},
    )
    orch = Orchestrator(oc_client=fake_client)
    result = orch.draft_spec(_evt(title="hi there", body="hi"))
    assert result.kind is not None
    assert result.kind.confidence < LOW_CONFIDENCE_NOTICE_THRESHOLD
    # Two comments — the spec_drafted summary + the low-confidence notice.
    assert len(result.comments) == 2


def test_empty_speckit_reply_escalates(fake_client):
    fake_client.set_command_result(
        "speckit.specify", {"parts": [{"type": "text", "text": ""}]}
    )
    orch = Orchestrator(oc_client=fake_client)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "escalated"
    assert result.skip_reason is SkipReason.EMPTY_DRAFT


def test_drafter_exception_escalates_cleanly(fake_client, monkeypatch):
    """A raise inside the drafter must not crash the orchestrator."""
    orch = Orchestrator(oc_client=fake_client)

    def _boom(*a, **kw):
        raise RuntimeError("opencode unreachable")

    monkeypatch.setattr(orch.drafter, "draft_design_spec", _boom)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "escalated"
    assert result.skip_reason is SkipReason.EMPTY_DRAFT
    assert "RuntimeError" in " ".join(result.notes)


def test_unhandled_event_type_returns_skipped(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    result = orch.draft_spec(_evt(type=EventType.CRON_SWEEP))
    assert result.status == "skipped"
    assert result.skip_reason is SkipReason.NOT_A_TRIGGER


def test_missing_issue_number_returns_skipped(fake_client):
    orch = Orchestrator(oc_client=fake_client)
    result = orch.draft_spec(_evt(issue=None))
    assert result.status == "skipped"


def test_classifier_kind_result_is_threaded_into_result(opencode_with_specify_reply):
    orch = Orchestrator(oc_client=opencode_with_specify_reply)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert isinstance(result.kind, KindResult)
    assert result.kind.kind is IntakeKind.FEATURE


# ---------------------------------------------------------------------------
# Tier 5: orchestrator threads dup-detection results into the drafted spec
# ---------------------------------------------------------------------------

def test_dup_detector_threads_callout_and_title_prefix(opencode_with_specify_reply):
    """When the dup detector returns a high-score candidate, the drafted
    spec body grows a callout AND the drafted title_prefix carries
    `[possible-dup]` (consumed by `_intake_title_from_result` in app.py)."""
    from agents.spec_generator.dup_detector import (
        BagOfWordsKnowledgeBase,
        DuplicateCandidate,
    )

    kb = BagOfWordsKnowledgeBase()
    # Seed with a near-identical existing spec so cosine >= 0.85 fires.
    kb.add(
        DuplicateCandidate(
            kind="spec",
            ref="docs/superpowers/specs/2026-01-01-csv-export-design.md",
            title="Existing CSV export spec",
            score=0.0,
        ),
        text="Add CSV export Please add a CSV download for sale orders",
    )
    orch = Orchestrator(
        oc_client=opencode_with_specify_reply, knowledge_base=kb,
    )
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "drafted"
    assert result.drafted is not None
    # Title prefix lands so app.py can prepend it to the PR title.
    assert result.drafted.title_prefix == "[possible-dup]"
    # Callout sits at the top of the drafted body for reviewers.
    assert "Possible duplicates" in result.drafted.body
    assert "2026-01-01-csv-export-design.md" in result.drafted.body


def test_dup_detector_skipped_silently_without_kb(opencode_with_specify_reply):
    """No KB wired -> no dup-detection attempted, no errors, normal draft."""
    orch = Orchestrator(oc_client=opencode_with_specify_reply)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "drafted"
    assert result.drafted is not None
    assert result.drafted.title_prefix == ""
    assert "Possible duplicates" not in result.drafted.body


def test_dup_detector_below_threshold_renders_callout_no_prefix(
    opencode_with_specify_reply,
):
    """A weak match (< 0.85) shows the candidate as a suggestion in the
    body, but the title is NOT prefixed (reporter judges)."""
    from agents.spec_generator.dup_detector import (
        BagOfWordsKnowledgeBase,
        DuplicateCandidate,
    )

    kb = BagOfWordsKnowledgeBase()
    # Loose lexical overlap — cosine well below 0.85.
    kb.add(
        DuplicateCandidate(
            kind="open_issue",
            ref="o/r#7",
            title="Unrelated tangent",
            score=0.0,
        ),
        text="Completely different words about CSV maybe",
    )
    orch = Orchestrator(
        oc_client=opencode_with_specify_reply, knowledge_base=kb,
    )
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.drafted is not None
    # No prefix even though a candidate appeared.
    assert result.drafted.title_prefix == ""
    # Callout still rendered (so the reporter sees the suggestion).
    assert "Possible duplicates" in result.drafted.body


def test_dup_detector_exception_does_not_block_draft(
    opencode_with_specify_reply, monkeypatch,
):
    """A KB failure must not block the drafter — draft proceeds without a callout."""
    from agents.spec_generator.dup_detector import KnowledgeBase

    class _BoomKB:
        def query(self, text, *, top_k=3):
            raise RuntimeError("kb down")

    kb: KnowledgeBase = _BoomKB()  # type: ignore[assignment]
    orch = Orchestrator(oc_client=opencode_with_specify_reply, knowledge_base=kb)
    result = orch.draft_spec(_evt(kind_hint="feature-request"))
    assert result.status == "drafted"
    assert result.drafted is not None
    assert result.drafted.title_prefix == ""
    assert "Possible duplicates" not in result.drafted.body
