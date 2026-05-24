"""Unit tests for the Tier 5 chatbot inbound intake."""

from __future__ import annotations

import hashlib
import hmac
import json

from agents.spec_generator.chatbot_intake import (
    ChatbotPayload,
    event_from_payload,
    parse_payload,
    verify_signature,
)
from agents.spec_generator.events import EventType


def _signed(body: bytes, secret: str) -> str:
    h = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={h}"


def test_verify_signature_accepts_correct_hmac():
    body = b'{"x": 1}'
    assert verify_signature(body=body, signature=_signed(body, "s3"), secret="s3")


def test_verify_signature_rejects_wrong_hmac():
    body = b'{"x": 1}'
    assert not verify_signature(
        body=body, signature="sha256=deadbeef", secret="s3"
    )


def test_verify_signature_rejects_unknown_prefix():
    body = b'{"x": 1}'
    assert not verify_signature(
        body=body, signature="md5=whatever", secret="s3"
    )


def test_parse_payload_round_trips_basic_shape():
    body = json.dumps({
        "conversation_id": "conv-1",
        "reporter": "alice@example.com",
        "title": "Add CSV export",
        "body": "Please add a CSV download.",
        "suggested_kind": "feature-request",
        "attachments": ["https://uploads/1.png"],
    }).encode("utf-8")
    payload = parse_payload(body)
    assert isinstance(payload, ChatbotPayload)
    assert payload.title == "Add CSV export"
    assert payload.suggested_kind == "feature-request"
    assert payload.attachments == ("https://uploads/1.png",)


def test_parse_payload_rejects_missing_title():
    body = json.dumps({"body": "hi"}).encode("utf-8")
    assert parse_payload(body) is None


def test_parse_payload_rejects_non_json():
    assert parse_payload(b"this is not json") is None


def test_event_from_payload_preserves_source_chatbot_label():
    payload = ChatbotPayload(
        conversation_id="conv-1",
        reporter="alice",
        title="Add CSV export",
        body="b",
        suggested_kind="feature-request",
        attachments=(),
    )
    event = event_from_payload(payload, issue_number=42)
    assert event.type is EventType.ISSUE_OPENED
    assert event.issue == 42
    assert event.kind_hint == "feature-request"
    labels = [item["name"] for item in event.raw["labels"]]
    assert "source:chatbot" in labels
    assert "feature-request" in labels


def test_event_from_payload_handles_no_suggested_kind():
    payload = ChatbotPayload(
        conversation_id="c", reporter="a", title="t", body="b",
        suggested_kind=None, attachments=(),
    )
    event = event_from_payload(payload, issue_number=99)
    assert event.kind_hint is None
    labels = [item["name"] for item in event.raw["labels"]]
    assert labels == ["source:chatbot"]
