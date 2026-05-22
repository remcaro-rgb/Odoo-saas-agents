# Odoo SaaS — Implementation Agent

An autonomous agent that turns a reporter-confirmed spec into a reviewed, preview-validated
pull request for the Odoo SaaS. This repo is the **alternative build** of that agent —
**OpenCode + Spec-Kit** wrapped by a thin Python orchestrator — rather than the bespoke
portable-runtime design.

> Design doc: `Odoo/docs/2026-05-20-implementation-agent-alt-design.md`
> (companion infographic: `…-alt-infographic.html`).

## Status — Phases A–F built; Tier 1 (runnable layer) wired

All six phases of the orchestrator (`agents/implementation/`) are built and unit-tested,
and the OpenCode service is deployed and verified live. **Tier 1** adds the runnable
layer — a composition root, the `python -m agents.implementation` entry point, and three
GitHub Actions trigger workflows — turning the tested library into an agent that fires
on a GitHub event.

To make the agent *live* you must provision the `implementation-bot` account, deploy the
trigger workflows into the data-plane repo, and set the repo secrets, then run the canary
rollout. **See [`docs/TIER-1-RUNBOOK.md`](docs/TIER-1-RUNBOOK.md)** (and
[`docs/PHASE-A.md`](docs/PHASE-A.md) for the OpenCode service).

## Architecture (one paragraph)

GitHub events → a thin **Python orchestrator** (this repo, `agents/`) → a long-running,
headless **OpenCode** service (`opencode/`) that runs **Spec-Kit's** `plan → tasks →
analyze → implement` workflow. OpenCode's model is the **OpenCode Go** subscription for
routine work, with a **frontier model (Claude)** for hard tasks. A hand-written
`coder.py` Odoo-specialization layer (Phase C) keeps Odoo correctness deterministic.
Per-spec Fly preview envs, the reporter loop and the PR/label state machine are carried
over unchanged from the current plan.

## Repo layout

```
opencode/            The long-running headless OpenCode service
  Dockerfile           pinned opencode-ai + @ai-sdk/openai-compatible
  fly.toml             long-running Fly app + durable session volume
  opencode.json        providers (OpenCode Go + frontier) + permission deny-list
  AGENTS.md            Odoo-specific coding rules for OpenCode
agents/
  implementation/      the orchestrator brain (Phases A–F) + the runnable layer
    app.py               composition root + `python -m agents.implementation`
    core.py              event routing + the plan / tasks / analyze pipeline
    coder.py             the Odoo specialization + validation layer
    opencode_client.py   thin HTTP client for the headless OpenCode server
    ...                  speckit_driver, gate1, preview, classifier, rollout, …
deploy/workflows/     GitHub Actions trigger workflows (deploy to the data-plane repo)
.specify/             Spec-Kit scaffold (stock templates + scripts)
  memory/constitution.md   the project constitution (policy-as-code)
tests/
  fixtures/            a trivial fixture spec
  smoke/               the Phase-A end-to-end smoke test
docs/PHASE-A.md       Phase-A runbook: secrets, deploy, smoke, spike checklist
docs/TIER-1-RUNBOOK.md  Tier-1 runbook: entry point, workflows, the bot account
.env.example          the secrets you must provide (no values)
```

## What is and isn't done

| | |
|---|---|
| ✅ Built & tested | The full orchestrator (Phases A–F), the runnable layer (`app.py`, `__main__.py`), the trigger workflows — 220 unit tests, ruff + mypy clean |
| ✅ Live | The OpenCode service on Fly — Phase-A smoke, the B→C run, and the model-portability proof all verified |
| ⛔ Needs you | The `implementation-bot` account; deploying the workflows; setting the repo secrets / variables |
| ⏭ Later (Tier 2–3) | agentlab (Gate 1), preview-env infra, the Notifier webhook, the multi-week canary rollout |

Next step: follow [`docs/TIER-1-RUNBOOK.md`](docs/TIER-1-RUNBOOK.md).
