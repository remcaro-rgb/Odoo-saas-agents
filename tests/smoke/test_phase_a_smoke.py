"""Phase-A end-to-end smoke test.

Proves the harness works: a headless OpenCode service takes a trivial spec and
produces a file edit. This is the Phase-A acceptance criterion.

It SKIPS cleanly when there is no live OpenCode service, so ``pytest`` stays green
on a bare checkout. To run it for real, stand the service up per ``docs/PHASE-A.md``
and point ``OPENCODE_BASE_URL`` at it:

    pip install -e ".[dev]"
    export OPENCODE_BASE_URL=https://<your-fly-app>.fly.dev
    export OPENCODE_SERVER_PASSWORD=...  OPENCODE_GO_API_KEY=...
    pytest -m smoke -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agents.implementation import OpenCodeClient, OpenCodeError

pytestmark = pytest.mark.smoke

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "hello-spec-design.md"


@pytest.fixture()
def client():
    """A live OpenCodeClient, or skip the test if the harness is not up."""
    oc = OpenCodeClient()
    if not oc.health():
        base = oc.base_url
        oc.close()
        pytest.skip(
            f"No live OpenCode service at {base} — "
            "stand one up per docs/PHASE-A.md, then re-run."
        )
    if not os.environ.get("OPENCODE_GO_API_KEY"):
        oc.close()
        pytest.skip("OPENCODE_GO_API_KEY not set — the service has no model to call.")
    yield oc
    oc.close()


def test_implement_trivial_spec_end_to_end(client: OpenCodeClient) -> None:
    """OpenCode takes the fixture spec and produces a file edit end-to-end."""
    spec = FIXTURE.read_text(encoding="utf-8")

    session = client.create_session(title="phase-a-smoke")
    prompt = (
        "Implement the following spec in the current workspace. "
        "Create the file it describes, with exactly the specified contents.\n\n"
        f"{spec}"
    )

    try:
        result = client.send_message(session.id, prompt)
    except OpenCodeError as exc:  # pragma: no cover - exercised only against a live service
        pytest.fail(f"send_message failed: {exc}")

    assert result, "OpenCode returned an empty response"

    diff = client.get_diff(session.id)
    assert diff, "OpenCode produced no file changes for the fixture spec"

    touched = " ".join(str(entry) for entry in diff)
    assert "hello.txt" in touched, f"expected hello.txt in the diff, got: {touched[:300]}"
