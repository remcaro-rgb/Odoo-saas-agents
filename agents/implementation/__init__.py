"""Implementation Agent — the orchestrator package.

- Phase A: `opencode_client` — HTTP client for the headless OpenCode service.
- Phase B: `events`, `spec_mapper`, `workspace`, `speckit_driver`, `core` — the
  orchestrator and the plan/tasks/analyze pipeline.
- Phase C: `odoo_rules`, `coder` — the deterministic Odoo specialization layer.
- Phase D (in progress): `classifier`, `commenter` + `core.reporter_iteration` —
  the reporter-iteration brain; the preview-env + workspace-provisioning infra
  is still pending.

Phases E–F (human-commit/observability, canary) are not built yet.
"""

from .classifier import Classifier, CommentIntent, HeuristicClassifier
from .coder import Coder
from .core import Orchestrator, route
from .events import Event, EventType, SpecKind
from .opencode_client import OpenCodeClient, OpenCodeError, Session
from .speckit_driver import SpecKitDriver
from .workspace import InMemoryWorkspace, Workspace

__all__ = [
    "Classifier",
    "Coder",
    "CommentIntent",
    "Event",
    "EventType",
    "HeuristicClassifier",
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
