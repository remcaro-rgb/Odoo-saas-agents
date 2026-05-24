"""Spec Generator Agent — the front-half of the SDD workflow.

The Implementation Agent runs Spec-Kit's back half (`/plan` -> `/tasks` ->
`/analyze` -> `/implement`). The Spec Generator runs the front half
(`/specify` -> `/clarify`): it converts a free-form GitHub Issue into a
well-structured design spec (for `feature-request`) or fix-brief (for `bug`)
on a new `agent/spec-<NNN>` branch, opens a PR, and stays available to iterate
with the reporter until the spec lands `intent-confirmed` — at which point the
Implementation Agent picks the work up.

This package is the *Tier 1 shadow* slice — the skeleton, the SHADOW-only
draft-design-spec flow, and the GitHub Actions entry point. Bug repro, the
auto-confirm sweep, the cron sweeper, dup detection, and the chatbot webhook
ship in later tiers (see ``docs/superpowers/plans/2026-05-23-spec-generator-agent.md``).

Reuse pattern: Tier 1 imports `OpenCodeClient`, `Rollout`, `RolloutDecision`,
and `EventLog` from the sibling `agents.implementation` package — they are the
exact same wire protocols and policy gates. A future shared-package refactor
(plan Q1, option a) can lift them into `agents/shared/` without changing the
Spec Generator's public surface.
"""

from __future__ import annotations

from .app import AgentConfig, main, run
from .classifier import (
    Classifier,
    HeuristicClassifier,
    IntakeKind,
    KindResult,
)
from .commenter import (
    AGENT_MARKER,
    awaiting_reporter_confirm,
    sensitive_escalation_notice,
    spec_drafted,
)
from .core import (
    DraftResult,
    Orchestrator,
    SkipReason,
    feature_name,
)
from .drafter import DraftedSpec, Drafter
from .events import Event, EventType
from .github_adapter import event_from_webhook
from .github_io import (
    FakeIssueClient,
    GhCliIssueClient,
    IssueClient,
    ShadowIssueClient,
    handle_webhook,
)
from .intake import Intake, IntakeBuilder
from .pushback import push_spec
from .refiner import (
    AWAITING_CONFIRM_LABEL,
    AWAITING_RECONFIRM_LABEL,
    INTENT_CONFIRMED_LABEL,
    CommentClassifier,
    CommentIntent,
    HeuristicCommentClassifier,
    IterationOutcome,
    Refiner,
)
from .run_store import (
    PHASE_AWAITING_REPORTER_CONFIRM,
    PHASE_COMPLETED,
    PHASE_DRAFTED,
    PHASE_ESCALATED,
    PHASE_INTENT_CONFIRMED,
    SOURCE_CHATBOT,
    SOURCE_EMAIL,
    SOURCE_GITHUB_ISSUE,
    DraftRecord,
    InMemoryRunStore,
    NoOpRunStore,
    PostgresRunStore,
    RunStore,
    build_run_store,
)
from .session_store import (
    InMemorySessionStore,
    JsonFileSessionStore,
    SessionStore,
)
from .speckit_driver import SpecKitFrontDriver

__all__ = [
    "DraftRecord",
    "InMemoryRunStore",
    "NoOpRunStore",
    "PHASE_AWAITING_REPORTER_CONFIRM",
    "PHASE_COMPLETED",
    "PHASE_DRAFTED",
    "PHASE_ESCALATED",
    "PHASE_INTENT_CONFIRMED",
    "PostgresRunStore",
    "RunStore",
    "SOURCE_CHATBOT",
    "SOURCE_EMAIL",
    "SOURCE_GITHUB_ISSUE",
    "build_run_store",
    "AGENT_MARKER",
    "AWAITING_CONFIRM_LABEL",
    "AWAITING_RECONFIRM_LABEL",
    "AgentConfig",
    "Classifier",
    "CommentClassifier",
    "CommentIntent",
    "DraftResult",
    "DraftedSpec",
    "Drafter",
    "Event",
    "EventType",
    "FakeIssueClient",
    "GhCliIssueClient",
    "HeuristicClassifier",
    "HeuristicCommentClassifier",
    "INTENT_CONFIRMED_LABEL",
    "InMemorySessionStore",
    "Intake",
    "IntakeBuilder",
    "IntakeKind",
    "IssueClient",
    "IterationOutcome",
    "JsonFileSessionStore",
    "KindResult",
    "Orchestrator",
    "Refiner",
    "SessionStore",
    "ShadowIssueClient",
    "SkipReason",
    "SpecKitFrontDriver",
    "awaiting_reporter_confirm",
    "event_from_webhook",
    "feature_name",
    "handle_webhook",
    "main",
    "push_spec",
    "run",
    "sensitive_escalation_notice",
    "spec_drafted",
]
