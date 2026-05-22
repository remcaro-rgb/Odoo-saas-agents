"""Implementation Agent — the orchestrator package.

- Phase A: `opencode_client` — HTTP client for the headless OpenCode service.
- Phase B: `events`, `spec_mapper`, `workspace`, `speckit_driver`, `core` — the
  orchestrator and the plan/tasks/analyze pipeline.
- Phase C: `odoo_rules`, `coder`, `gate1` — the Odoo specialization layer plus
  the build/lint/test quality gate.
- Phase D: `classifier`, `commenter`, `core.reporter_iteration` — the reporter-
  iteration brain; `github_adapter`, `github_io` — the GitHub I/O layer (webhook
  in, comments/labels out); `provisioning` — OpenCode on a per-spec git checkout;
  `preview` — per-spec Fly preview environments.

Phases E–F (human-commit/observability, canary) are not built yet.
"""

from .classifier import Classifier, CommentIntent, HeuristicClassifier
from .coder import Coder
from .core import Orchestrator, route
from .events import Event, EventType, SpecKind
from .gate1 import CheckRunner, FakeCheckRunner, Gate1
from .github_adapter import event_from_webhook
from .github_io import FakeGitHubClient, GitHubClient, handle_webhook
from .opencode_client import OpenCodeClient, OpenCodeError, Session
from .preview import FakeFlyClient, FlyClient, PreviewEnv, PreviewManager
from .speckit_driver import SpecKitDriver
from .workspace import InMemoryWorkspace, Workspace

__all__ = [
    "CheckRunner",
    "Classifier",
    "Coder",
    "CommentIntent",
    "Event",
    "EventType",
    "FakeCheckRunner",
    "FakeFlyClient",
    "FakeGitHubClient",
    "FlyClient",
    "Gate1",
    "GitHubClient",
    "HeuristicClassifier",
    "InMemoryWorkspace",
    "OpenCodeClient",
    "OpenCodeError",
    "Orchestrator",
    "PreviewEnv",
    "PreviewManager",
    "Session",
    "SpecKind",
    "SpecKitDriver",
    "Workspace",
    "event_from_webhook",
    "handle_webhook",
    "route",
]
