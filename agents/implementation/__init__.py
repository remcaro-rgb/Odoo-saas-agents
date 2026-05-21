"""Implementation Agent — the orchestrator package.

Phase A ships only `opencode_client.py` (the HTTP client for the headless
OpenCode service). Phases B–C add `core.py`, `speckit_driver.py`, and `coder.py`.
"""

from .opencode_client import OpenCodeClient, OpenCodeError, Session

__all__ = ["OpenCodeClient", "OpenCodeError", "Session"]
