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

# A non-bot push to a branch under this prefix is a human commit (design §5.3).
_AGENT_BRANCH_PREFIX = "agent/"
# Bot service accounts whose pushes are the agents' own work, not a human's.
_BOT_LOGINS = frozenset({"implementation-bot", "spec-generator-bot"})


def event_from_webhook(event_name: str, payload: dict[str, Any]) -> Event | None:
    """Map a GitHub webhook (the `X-GitHub-Event` name + its JSON payload) to an
    `Event`, or `None` for webhooks the orchestrator does not act on."""
    if event_name == "issue_comment":
        return _issue_comment_event(payload)
    if event_name == "pull_request":
        return _pull_request_event(payload)
    if event_name == "push":
        return _push_event(payload)
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


def _push_event(payload: dict[str, Any]) -> Event | None:
    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/"):
        return None  # a tag or other ref — not a branch push
    branch = ref[len("refs/heads/") :]
    if not branch.startswith(_AGENT_BRANCH_PREFIX):
        return None  # not an agent branch — not the orchestrator's concern
    actor = (payload.get("sender") or {}).get("login")
    # A GitHub App pushes as "<name>[bot]" — normalise before the bot check so
    # the agent's own commits are never misread as a human commit.
    if actor and actor.removesuffix("[bot]") in _BOT_LOGINS:
        return None  # the agent's own push, not a human commit
    return Event(
        type=EventType.HUMAN_PUSH,
        branch=branch,
        actor=actor,
        raw=payload,
    )
