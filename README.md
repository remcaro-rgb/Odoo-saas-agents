# Odoo SaaS — Implementation Agent

An autonomous agent that turns a reporter-confirmed spec into a reviewed, preview-validated
pull request for the Odoo SaaS. This repo is the **alternative build** of that agent —
**OpenCode + Spec-Kit** wrapped by a thin Python orchestrator — rather than the bespoke
portable-runtime design.

> Design doc: `Odoo/docs/2026-05-20-implementation-agent-alt-design.md`
> (companion infographic: `…-alt-infographic.html`).

## Status — Phase A (Headless harness): scaffolded

This repo currently contains the **Phase A scaffold only**. It is **not yet a working
agent** and **not yet deployed**. Phase A stands up the harness everything else sits on;
Phases B–F (the orchestrator logic) are not built yet.

To make Phase A *live* you must provision three things this scaffold cannot create —
an **OpenCode Go subscription**, an **Anthropic API key**, and a **Fly.io account/token** —
then deploy and run the smoke test. **See [`docs/PHASE-A.md`](docs/PHASE-A.md).**

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
  implementation/
    opencode_client.py thin HTTP client for the headless OpenCode server
.specify/             Spec-Kit scaffold (stock templates + scripts)
  memory/constitution.md   the project constitution (policy-as-code)
tests/
  fixtures/            a trivial fixture spec
  smoke/               the Phase-A end-to-end smoke test
docs/PHASE-A.md       Phase-A runbook: secrets, deploy, smoke, spike checklist
.env.example          the secrets you must provide (no values)
```

## What is and isn't done

| | |
|---|---|
| ✅ Scaffolded | Dockerfile, fly.toml, opencode.json, AGENTS.md, constitution, `opencode_client.py`, `.specify/`, the smoke test + fixture |
| ⛔ Needs you | OpenCode Go subscription, Anthropic key, Fly account/token; the deploy; running the smoke test |
| ⏭ Later phases | the orchestrator (`core.py`, `speckit_driver.py`), `coder.py`, preview envs, the reporter loop (Phases B–F) |

Next step: follow [`docs/PHASE-A.md`](docs/PHASE-A.md).
