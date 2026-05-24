"""Map a GitHub Issues webhook payload onto a normalized `Event`.

The Spec Generator's orchestrator core routes on `Event`s (see `events.py`).
This adapter is the GitHub-specific front door — payload in, `Event` out, no
I/O. Sibling of `agents.implementation.github_adapter`; the two adapters
target *different* webhook families (Issues here, PRs there).
"""

from __future__ import annotations

from typing import Any

from .events import Event, EventType

# Labels that hint at kind. The classifier still gets the final say.
ROUTING_LABELS = frozenset({"feature-request", "bug", "spec-refinement-needed"})


def event_from_webhook(event_name: str, payload: dict[str, Any]) -> Event | None:
    """Map a GitHub Issues webhook to an `Event`, or `None` to skip.

    Routes the four Issues webhook actions the Spec Generator cares about
    in Tier 1: `opened`, `labeled`, `issue_comment.created`. Other webhooks
    return `None` — silently drop, the workflow can over-subscribe safely.
    """
    if event_name == "issues":
        return _issues_event(payload)
    if event_name == "issue_comment":
        return _issue_comment_event(payload)
    return None


def _issues_event(payload: dict[str, Any]) -> Event | None:
    action = payload.get("action")
    if action not in ("opened", "labeled"):
        return None
    issue = payload.get("issue") or {}
    # A PR's `pull_request` field is also delivered as an Issues webhook — the
    # Spec Generator never acts on PRs (the Implementation Agent does), so
    # drop them here.
    if "pull_request" in issue:
        return None

    label = (payload.get("label") or {}).get("name") if action == "labeled" else None
    # On `labeled`, only the routing labels are interesting — anything else
    # is noise (e.g. priority labels). On `opened`, kind_hint comes from the
    # full initial label set if any of them are routing labels.
    if action == "labeled" and label not in ROUTING_LABELS:
        return None
    if action == "opened":
        labels = [
            (item.get("name") or "").lower()
            for item in (issue.get("labels") or [])
            if isinstance(item, dict)
        ]
        kind_hint = next(
            (lbl for lbl in labels if lbl in ROUTING_LABELS), None
        )
    else:
        kind_hint = label

    return Event(
        type=EventType.ISSUE_OPENED if action == "opened" else EventType.ISSUE_LABELED,
        issue=issue.get("number"),
        title=issue.get("title"),
        body=issue.get("body"),
        actor=(issue.get("user") or {}).get("login"),
        kind_hint=kind_hint,
        raw=payload,
    )


def _issue_comment_event(payload: dict[str, Any]) -> Event | None:
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue") or {}
    # `issue_comment` carries the same shape whether the comment is on an
    # issue or a PR. Tier 1 reporter Q&A keys off PR comments (the spec PR
    # is the iteration locus); a plain-issue comment can still be useful
    # to surface, so keep both — the orchestrator decides what to do.
    comment = payload.get("comment") or {}
    return Event(
        type=EventType.ISSUE_COMMENT,
        issue=issue.get("number"),
        pr=issue.get("number") if "pull_request" in issue else None,
        title=issue.get("title"),
        body=issue.get("body"),
        comment=comment.get("body"),
        actor=(comment.get("user") or {}).get("login"),
        raw=payload,
    )
