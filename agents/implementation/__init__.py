"""Implementation Agent — the orchestrator package.

- Phase A: `opencode_client` — HTTP client for the headless OpenCode service.
- Phase B: `events`, `spec_mapper`, `workspace`, `speckit_driver`, `core` — the
  orchestrator and the plan/tasks/analyze pipeline.
- Phase C: `odoo_rules`, `coder` — the deterministic Odoo specialization layer.

Phases D–F (preview envs, reporter loop, canary) are not built yet.
"""

from .coder import Coder
from .core import Orchestrator, route
from .events import Event, EventType, SpecKind
from .opencode_client import OpenCodeClient, OpenCodeError, Session
from .speckit_driver import SpecKitDriver
from .workspace import InMemoryWorkspace, Workspace

__all__ = [
    "Coder",
    "Event",
    "EventType",
    "InMemoryWorkspace",
    "OpenCodeClient",
    "OpenCodeError",
    "Orchestrator",
    "Session",
    "SpecKind",
    "SpecKitDriver",
    "Workspace",
    "route",
]
