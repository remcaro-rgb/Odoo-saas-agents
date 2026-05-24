"""Chatbot-gateway inbound intake (Tier 5).

The Support chatbot can route an interaction to the Spec Generator by POSTing
a normalised payload to a Vercel-hosted webhook that fronts a GitHub Action.
The Action calls into this module which:

  1. Verifies the HMAC signature on the inbound payload.
  2. Maps the chatbot's shape to an `Event` that looks like an
     `issues.opened` webhook with the `source:chatbot` kind hint preserved.
  3. Hands the event back to the orchestrator's normal dispatch path.

Q5 from the plan: re-classify (defence in depth) — the orchestrator's
classifier still gets to override the chatbot's `kind` hint when the body
contains contradictory signals (e.g., a `feature-request` hint on a body
that obviously screams `bug`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .events import Event, EventType


@dataclass(frozen=True)
class ChatbotPayload:
    """The contract the chatbot gateway POSTs to the webhook."""

    conversation_id: str       # chatbot-side conversation id (for trace)
    reporter: str              # the end-user's login / email
    title: str
    body: str
    suggested_kind: str | None  # "feature-request" | "bug" | None
    attachments: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "reporter": self.reporter,
            "title": self.title,
            "body": self.body,
            "suggested_kind": self.suggested_kind,
            "attachments": list(self.attachments),
        }


def verify_signature(*, body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 verification using the shared chatbot gateway secret.

    Signature header shape: `sha256=<hex>`. The constant-time compare
    guards against timing attacks per the design §8 hardening notes.
    """
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


def parse_payload(body: bytes) -> ChatbotPayload | None:
    """Decode the inbound JSON. Returns `None` for malformed payloads."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("title"), str) or not isinstance(data.get("body"), str):
        return None
    kind = data.get("suggested_kind")
    return ChatbotPayload(
        conversation_id=str(data.get("conversation_id", "")),
        reporter=str(data.get("reporter", "chatbot")),
        title=data["title"],
        body=data["body"],
        suggested_kind=kind if isinstance(kind, str) else None,
        attachments=tuple(
            str(url) for url in (data.get("attachments") or []) if isinstance(url, str)
        ),
    )


def event_from_payload(payload: ChatbotPayload, *, issue_number: int) -> Event:
    """Map a verified `ChatbotPayload` to an `Event` (ISSUE_OPENED shape).

    `issue_number` is the GitHub issue id assigned after the gateway mirrors
    the chatbot conversation as a real issue — the chatbot is responsible
    for creating it via the GitHub API before invoking the webhook. The
    `source:chatbot` label is preserved as a hint so the orchestrator
    records the funnel source on the `spec_generator_runs` row.
    """
    return Event(
        type=EventType.ISSUE_OPENED,
        issue=issue_number,
        title=payload.title,
        body=payload.body,
        actor=payload.reporter,
        kind_hint=payload.suggested_kind,
        raw={
            "source": "chatbot",
            "conversation_id": payload.conversation_id,
            "attachments": list(payload.attachments),
            # The downstream `IntakeBuilder` will read these out as if the
            # issue body itself carried them.
            "labels": (
                [{"name": "source:chatbot"}]
                + ([{"name": payload.suggested_kind}] if payload.suggested_kind else [])
            ),
        },
    )
