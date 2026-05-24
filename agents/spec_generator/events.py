"""Normalized Spec Generator events.

The Spec Generator's trigger surface is wider than the Implementation Agent's:
it reacts to `issues.opened`, `issues.labeled`, `issue_comment.created`, and a
cron sweep. Pure data structures — the orchestrator core routes on these. An
adapter (`github_adapter.event_from_webhook`) maps raw GitHub payloads onto
`Event`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """The trigger kinds the Spec Generator orchestrator understands."""

    # A new issue arrived — the primary path for the draft-spec flow.
    ISSUE_OPENED = "issue_opened"

    # An existing issue had a routing label applied (`feature-request`,
    # `bug`, `spec-refinement-needed`). Lets a human re-route an issue
    # the agent skipped or mis-classified.
    ISSUE_LABELED = "issue_labeled"

    # A comment on a tracked issue (or its spec PR) — the reporter Q&A
    # path (Tier 2 refiner).
    ISSUE_COMMENT = "issue_comment"

    # The nightly sweep: auto-confirm specs whose reporter has gone silent
    # for 24h with no open questions (Tier 3).
    CRON_SWEEP = "cron_sweep"


@dataclass
class Event:
    """A normalized Spec Generator trigger — adapter-agnostic.

    `kind_hint` is the routing label the webhook carried (`feature-request`,
    `bug`, `source:chatbot`, `spec-refinement-needed`). Lower-case, no
    backing enum — the classifier still gets the final say on category, and
    new labels show up routinely.
    """

    type: EventType
    issue: int | None = None
    pr: int | None = None
    title: str | None = None
    body: str | None = None
    comment: str | None = None
    actor: str | None = None
    kind_hint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
