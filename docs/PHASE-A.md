# Phase A — Headless harness · Runbook

Phase A stands up the harness the Implementation Agent runs on: a long-running,
headless **OpenCode** service configured with the **OpenCode Go** + frontier providers,
the **Spec-Kit** scaffold, the **constitution**, and a thin Python client. This runbook
covers what is scaffolded, what **you** must provision, how to deploy, and how to prove
it works.

## Status

**Scaffolded — not yet live.** Every code/config artifact exists in this repo. Three
things must be provisioned by you (accounts and secrets cannot be created for you), then
the service deployed and the smoke test run.

## What is scaffolded

| Path | What it is |
|---|---|
| `opencode/Dockerfile` | OpenCode service image — pinned `opencode-ai@1.15.7` + `@ai-sdk/openai-compatible@2.0.47` |
| `opencode/fly.toml` | long-running Fly app + durable session volume |
| `opencode/opencode.json` | OpenCode Go + frontier (Claude) providers; permission deny-list |
| `opencode/AGENTS.md` | Odoo coding rules for OpenCode |
| `.specify/` | Spec-Kit scaffold — stock templates + scripts |
| `.specify/memory/constitution.md` | the project constitution (policy-as-code) |
| `agents/implementation/opencode_client.py` | HTTP client for the headless server |
| `tests/smoke/test_phase_a_smoke.py` | the end-to-end acceptance test + fixture spec |

## 1. Provision (you)

Phase A cannot be completed without these — accounts cannot be created, and secrets
cannot be handled, on your behalf.

1. **OpenCode Go subscription.** Sign in at <https://opencode.ai/zen>, subscribe to
   "Go", copy the API key. → `OPENCODE_GO_API_KEY`
2. **Anthropic API key** (the frontier model). <https://console.anthropic.com> → API
   keys. → `ANTHROPIC_API_KEY`
3. **Fly.io account + token.** `fly auth signup` (or login), then
   `fly tokens create deploy`. → `FLY_API_TOKEN`
4. Choose an **`OPENCODE_SERVER_PASSWORD`** — any strong random string.

`.env.example` lists all of them.

## 2. Deploy the OpenCode service

```bash
cd opencode
fly apps create odoo-saas-opencode            # or edit `app` in fly.toml
fly volumes create opencode_data --region iad --size 3
fly secrets set \
  OPENCODE_GO_API_KEY=...   \
  ANTHROPIC_API_KEY=...     \
  OPENCODE_SERVER_PASSWORD=...
fly deploy
```

`fly deploy` builds `Dockerfile` and starts the long-running service. Verify:
`fly status` shows one running machine; `curl -u opencode:$OPENCODE_SERVER_PASSWORD
https://<app>.fly.dev/event` streams a `server.connected` event.

## 3. Run the smoke test

```bash
pip install -e ".[dev]"
export OPENCODE_BASE_URL=https://odoo-saas-opencode.fly.dev
export OPENCODE_SERVER_PASSWORD=...   OPENCODE_GO_API_KEY=...
pytest -m smoke -v
```

The smoke test creates a session, hands OpenCode the trivial fixture spec
(`tests/fixtures/hello-spec-design.md`), and asserts a file edit comes back. With no
live service it **skips** — so `pytest` is green on a bare checkout.

## 4. Phase-A spike checklist (the go/no-go evidence)

Phase A is the validation *spike*. Before committing to Phases B–F, confirm:

- [ ] Headless OpenCode can be driven reliably over HTTP for autonomous, no-human runs.
- [ ] The full Spec-Kit pipeline + preview spawn fits the < 30-minute SLA on a
      representative spec.
- [ ] The `opencode-ai` release cadence is stable enough to pin against.
- [ ] Benchmarking OpenCode Go vs Claude on real Odoo specs yields a workable
      frontier-escalation threshold.

## 5. Known VERIFY items

Scaffolded honestly — confirm these against current OpenCode docs before go-live:

- **Session data dir.** `Dockerfile` / `fly.toml` point `XDG_DATA_HOME` at the `/data`
  volume so sessions survive a restart. Confirm OpenCode honours it for the session DB.
- **Claude model id.** `opencode.json` uses `claude-sonnet-4-6` — set it to your current
  Claude model identifier.
- **Spec-Kit commands inside OpenCode.** Phase B installs the Spec-Kit command prompts as
  OpenCode custom commands; confirm the install mechanism (design-doc open question 1).
- **OpenCode version pin.** `1.15.7` was latest at scaffold time — pin deliberately and
  treat bumps as gated changes (open question 5).

## Not in Phase A

The orchestrator logic — `core.py`, `speckit_driver.py`, the `coder.py` Odoo layer, the
preview environment, the reporter loop — is Phases B–F. Phase A is only the harness.
