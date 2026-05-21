"""Normalized agent events and shared types (Phase B).

Pure data structures — the orchestrator core routes on these. An adapter
(GitHub webhooks, etc.) is responsible for mapping a raw payload onto an `Event`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """The trigger kinds the orchestrator understands."""

    INTENT_CONFIRMED = "intent_confirmed"  # a spec PR was marked intent-confirmed
    ISSUE_COMMENT = "issue_comment"        # reporter commented on the issue (Phase D)
    HUMAN_PUSH = "human_push"              # a non-agent push to agent/spec-* (Phase E)
    CRON_SWEEP = "cron_sweep"              # the stale-PR sweep (Phase E)


class SpecKind(StrEnum):
    """How heavy the spec is — decides whether the planning pipeline runs in full."""

    DESIGN = "design"   # heavyweight design spec -> full /plan + /tasks pipeline
    FIX = "fix"         # lightweight fix-brief  -> fast-path, skip /plan + /tasks


@dataclass
class Event:
    """A normalized agent trigger. Adapter-agnostic."""

    type: EventType
    branch: str | None = None
    issue: int | None = None
    pr: int | None = None
    spec_path: str | None = None
    comment: str | None = None
    actor: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
