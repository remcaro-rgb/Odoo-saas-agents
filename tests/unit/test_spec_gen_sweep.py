"""Unit tests for the Spec Generator auto-confirm sweep (Tier 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.spec_generator.sweep import (
    AUTO_CONFIRM_AFTER,
    FakeSweepClient,
    SweepResult,
    derive_spec_path,
    has_open_questions,
    run_sweep,
)


def test_derive_spec_path_recovers_design_spec():
    assert (
        derive_spec_path("agent/spec-0012-add-csv-export")
        == "docs/superpowers/specs/spec-0012-add-csv-export-design.md"
    )


def test_derive_spec_path_returns_none_for_non_agent_branch():
    assert derive_spec_path("main") is None


def test_has_open_questions_detects_both_marker_styles():
    assert has_open_questions("- [NEEDS CLARIFICATION: which TZ?]")
    assert has_open_questions("- [NEEDS INPUT: archived flag?]")


def test_has_open_questions_false_for_clean_spec():
    assert not has_open_questions("# Spec\n- captured A\n- captured B\n")


def test_silent_pr_with_no_questions_is_auto_confirmed():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    last_edit = now - timedelta(hours=30)  # silent for >24h
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "agent/spec-0007-csv"}],
        last_modified={(7, "docs/superpowers/specs/spec-0007-csv-design.md"): last_edit},
        contents={
            (7, "docs/superpowers/specs/spec-0007-csv-design.md"): "# spec\n- ok",
        },
    )
    result = run_sweep(client=client, now=now)
    assert isinstance(result, SweepResult)
    assert len(result.confirmed) == 1
    assert result.confirmed[0].action == "confirm"
    # The label + comment were applied.
    assert (7, "intent-confirmed") in client.labels_added
    assert client.comments_posted
    body = client.comments_posted[0][1]
    assert "7 days" in body  # the /reopen window callout


def test_recent_edit_is_skipped():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    last_edit = now - timedelta(hours=4)  # silent for only 4h, not 24h
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "agent/spec-0007-x"}],
        last_modified={(7, "docs/superpowers/specs/spec-0007-x-design.md"): last_edit},
        contents={(7, "docs/superpowers/specs/spec-0007-x-design.md"): "# spec\n"},
    )
    result = run_sweep(client=client, now=now)
    assert result.confirmed == []
    assert client.labels_added == []


def test_open_questions_block_auto_confirm():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    last_edit = now - AUTO_CONFIRM_AFTER - timedelta(hours=1)
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "agent/spec-0007-x"}],
        last_modified={(7, "docs/superpowers/specs/spec-0007-x-design.md"): last_edit},
        contents={
            (7, "docs/superpowers/specs/spec-0007-x-design.md"):
                "- [NEEDS CLARIFICATION: still need answer]",
        },
    )
    result = run_sweep(client=client, now=now)
    assert result.confirmed == []
    assert any("[NEEDS CLARIFICATION]" in d.reason for d in result.decisions)


def test_no_commit_history_is_skipped_gracefully():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "agent/spec-0007-x"}],
        last_modified={},
        contents={},
    )
    result = run_sweep(client=client, now=now)
    assert result.confirmed == []
    assert client.labels_added == []
    assert any("no commit history" in d.reason for d in result.decisions)


def test_non_agent_branch_is_classified_as_no_spec():
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "main"}],
    )
    result = run_sweep(client=client)
    assert result.decisions[0].action == "no-spec"


def test_dry_run_does_not_apply_label_or_comment():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    last_edit = now - AUTO_CONFIRM_AFTER - timedelta(hours=1)
    client = FakeSweepClient(
        prs=[{"number": 7, "headRefName": "agent/spec-0007-x"}],
        last_modified={(7, "docs/superpowers/specs/spec-0007-x-design.md"): last_edit},
        contents={(7, "docs/superpowers/specs/spec-0007-x-design.md"): "# ok\n"},
    )
    result = run_sweep(client=client, now=now, act=False)
    assert len(result.confirmed) == 1  # decision computed
    assert client.labels_added == []   # but no side-effects
    assert client.comments_posted == []
