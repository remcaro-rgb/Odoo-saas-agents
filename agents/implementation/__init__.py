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
- Phase E: `observability` — structured event logs; `notifier` — escalation
  routes; `cost` — per-PR cost tracking + spend cap; the human-commit flow
  (`github_adapter` push -> `github_io` reporter ping).

- Phase F: `rollout` — the 4-stage canary gate (shadow -> fixtures -> opt-in ->
  default-on) and the AGENTS_ENABLED kill switch.
"""

from .classifier import Classifier, CommentIntent, HeuristicClassifier
from .coder import Coder
from .core import Orchestrator, route
from .cost import Budget, session_cost
from .events import Event, EventType, SpecKind
from .gate1 import CheckRunner, FakeCheckRunner, Gate1
from .github_adapter import event_from_webhook
from .github_io import FakeGitHubClient, GitHubClient, handle_webhook
from .notifier import FakeNotifier, Notifier, notify_alert, notify_escalation
from .observability import EventLog
from .opencode_client import OpenCodeClient, OpenCodeError, Session
from .preview import FakeFlyClient, FlyClient, PreviewEnv, PreviewManager
from .rollout import Rollout, RolloutDecision, RolloutStage
from .speckit_driver import SpecKitDriver
from .workspace import InMemoryWorkspace, Workspace

__all__ = [
    "Budget",
    "CheckRunner",
    "Classifier",
    "Coder",
    "CommentIntent",
    "Event",
    "EventLog",
    "EventType",
    "FakeCheckRunner",
    "FakeFlyClient",
    "FakeGitHubClient",
    "FakeNotifier",
    "FlyClient",
    "Gate1",
    "GitHubClient",
    "HeuristicClassifier",
    "InMemoryWorkspace",
    "Notifier",
    "OpenCodeClient",
    "OpenCodeError",
    "Orchestrator",
    "PreviewEnv",
    "PreviewManager",
    "Rollout",
    "RolloutDecision",
    "RolloutStage",
    "Session",
    "SpecKind",
    "SpecKitDriver",
    "Workspace",
    "event_from_webhook",
    "handle_webhook",
    "notify_alert",
    "notify_escalation",
    "route",
    "session_cost",
]
