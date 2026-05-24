"""Unit tests for SpecKitFrontDriver — parses /speckit.specify output."""

from __future__ import annotations

from agents.spec_generator.speckit_driver import SpecifyResult, SpecKitFrontDriver


class _FakeClient:
    """Minimal stub matching the OpenCode client surface `SpecKitFrontDriver` calls."""

    def __init__(self, reply_text: str = "") -> None:
        self.reply_text = reply_text
        self.calls: list[dict[str, object]] = []

    def run_command(
        self, session_id: str, command: str, arguments: str = "", *, model=None
    ) -> dict[str, object]:
        self.calls.append(
            {
                "session_id": session_id,
                "command": command,
                "arguments": arguments,
                "model": model,
            }
        )
        return {"parts": [{"type": "text", "text": self.reply_text}]}


def test_run_specify_passes_arguments_through_and_returns_text():
    client = _FakeClient(reply_text="# Spec\n- captured item one\n- captured item two")
    result = SpecKitFrontDriver(client).run_specify(
        "sess-1", "Add CSV export"
    )
    assert isinstance(result, SpecifyResult)
    assert "captured item one" in result.spec_text
    assert client.calls[0]["command"] == "speckit.specify"
    assert client.calls[0]["arguments"] == "Add CSV export"


def test_run_specify_extracts_needs_clarification_markers():
    text = (
        "# CSV Export\n"
        "- a captured item\n"
        "- [NEEDS CLARIFICATION: which timezone for the export?]\n"
        "- [NEEDS INPUT: include archived records?]\n"
    )
    result = SpecKitFrontDriver(_FakeClient(text)).run_specify("s", "x")
    assert result.open_questions == (
        "which timezone for the export?",
        "include archived records?",
    )


def test_run_specify_dedupes_open_questions():
    text = (
        "[NEEDS CLARIFICATION: same question?]\n"
        "[NEEDS CLARIFICATION: same question?]\n"
    )
    result = SpecKitFrontDriver(_FakeClient(text)).run_specify("s", "x")
    assert result.open_questions == ("same question?",)


def test_empty_reply_returns_empty_spec_text():
    result = SpecKitFrontDriver(_FakeClient("")).run_specify("s", "x")
    assert result.spec_text == ""
    assert result.open_questions == ()


def test_run_clarify_uses_clarify_command():
    client = _FakeClient(reply_text="updated spec")
    SpecKitFrontDriver(client).run_clarify("sess-1", "Yes, GMT timezone.")
    assert client.calls[0]["command"] == "speckit.clarify"


def test_model_argument_threads_through():
    client = _FakeClient()
    SpecKitFrontDriver(client).run_specify(
        "s", "x", model="opencode-go/deepseek-v4-pro"
    )
    assert client.calls[0]["model"] == "opencode-go/deepseek-v4-pro"
