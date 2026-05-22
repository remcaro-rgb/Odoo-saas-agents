"""Map a GitHub webhook payload onto a normalized `Event` (Phase D).

The orchestrator core routes on `Event`s (see `events.py`); this adapter is the
GitHub-specific front door. Pure — payload in, `Event` out, no I/O. Fields the
webhook does not carry (the PR head branch on an `issue_comment`; the spec path
on a labeled PR) are left `None` for the caller to resolve against the GitHub API.
"""

from __future__ import annotations

from typing import Any

from .events import Event, EventType

# A spec PR is treated as "intent-confirmed" once this label is applied.
INTENT_CONFIRMED_LABEL = "intent-confirmed"


def event_from_webhook(event_name: str, payload: dict[str, Any]) -> Event | None:
    """Map a GitHub webhook (the `X-GitHub-Event` name + its JSON payload) to an
    `Event`, or `None` for webhooks the orchestrator does not act on."""
    if event_name == "issue_comment":
        return _issue_comment_event(payload)
    if event_name == "pull_request":
        return _pull_request_event(payload)
    return None


def _issue_comment_event(payload: dict[str, Any]) -> Event | None:
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue") or {}
    if "pull_request" not in issue:
        return None  # a comment on a plain issue, not a PR — not a reporter iteration
    comment = payload.get("comment") or {}
    return Event(
        type=EventType.ISSUE_COMMENT,
        pr=issue.get("number"),
        comment=comment.get("body"),  # None ok — an empty body classifies as NOISE
        actor=(comment.get("user") or {}).get("login"),
        raw=payload,
    )


def _pull_request_event(payload: dict[str, Any]) -> Event | None:
    if payload.get("action") != "labeled":
        return None
    if (payload.get("label") or {}).get("name") != INTENT_CONFIRMED_LABEL:
        return None
    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    return Event(
        type=EventType.INTENT_CONFIRMED,
        pr=pull_request.get("number"),
        branch=head.get("ref"),
        actor=(payload.get("sender") or {}).get("login"),
        raw=payload,
    )
