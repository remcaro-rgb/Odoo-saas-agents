# Spec Generator Tier 1 — Make it runnable in SHADOW · Runbook

Tier 1 of the Spec Generator stands up the package, the composition root, the
shadow GitHub client, and the smallest possible flow (`draft_design_spec` for a
feature-request) end-to-end in SHADOW mode. Everything runnable, nothing
posted.

This document mirrors `docs/TIER-1-RUNBOOK.md` (the Implementation Agent's
Tier-1 runbook). Most of the operational shape is identical — the two agents
ride the same OpenCode + Spec-Kit harness and the same `Rollout` canary gate.

## Status

**Built and unit-tested — not yet wired live.** The package
(`agents/spec_generator/`), the `python -m agents.spec_generator` entry point,
and the trigger workflow (`deploy/workflows/spec-generator.yml`) exist and
pass 71 unit tests in addition to the 281 baseline tests of the
Implementation Agent — full suite is green. To make Tier 1 *live in SHADOW*
on the data-plane repo you must do two things this code cannot do for you:
deploy the workflow into the data-plane repo's `.github/workflows/` (§3) and
set the secrets / variables (§4). The `spec-generator-bot` GitHub App is NOT
required for SHADOW — that lands in Tier 2 (§7).

## 1. What Tier 1 delivers

| Piece | File | Role |
|---|---|---|
| Package skeleton | `agents/spec_generator/*` | 11 modules: events, intake, classifier, drafter, commenter, core, speckit_driver, github_adapter, github_io, app, plus `__main__`. |
| Composition root | `agents/spec_generator/app.py` | Reads the event, consults the rollout gate, builds the real clients, calls `handle_webhook`. |
| Entry point | `agents/spec_generator/__main__.py` | `python -m agents.spec_generator`. |
| Shadow client | `github_io.ShadowIssueClient` | Real reads, recorded-but-unsent writes — the SHADOW stage. |
| Trigger workflow | `deploy/workflows/spec-generator.yml` | Fires on `issues.opened` / `issues.labeled`. |
| Reuse from impl agent | `agents.implementation.{opencode_client,rollout,observability,notifier}` | Same wire protocols + policy gates. Cross-package import — no shared package refactor yet (see §8). |

The agent runs as an **ephemeral GitHub Action** — one process per webhook —
not as an always-on service. The only long-running service is the OpenCode Fly
app (`opencode/`), which the Implementation Agent already runs.

## 2. How the agent runs

A workflow runs `python -m agents.spec_generator`. GitHub Actions hands every
step the event name and a JSON payload file via `$GITHUB_EVENT_NAME` /
`$GITHUB_EVENT_PATH`. `app.run()`:

1. Loads the event. No event → exits 0 (nothing to do).
2. Builds `Rollout.from_env()` and decides **ACT / SHADOW / SKIP** for the target.
3. SKIP → exits 0. Otherwise builds `OpenCodeClient`, the `Orchestrator`, and
   a `IssueClient` (`GhCliIssueClient` for ACT, `ShadowIssueClient` for SHADOW),
   then calls `handle_webhook`.
4. Emits structured JSON log records (`observability.EventLog`) throughout,
   tagged with `agent_override="spec_generator"` so the Better Stack drain can
   filter against the Implementation Agent's records.

Exit code: **0** for a clean run — a sensitive-content escalation, a config
question routed to support, and a `Tier-4-deferred bug intake` ALL count as
clean. **1** is reserved for a missing-config abort or an unhandled error so a
red Action means a genuine failure.

## 3. Deploy the trigger workflow

The workflow file lives in this repo for version control but is **inert here**
— it is at `deploy/workflows/spec-generator.yml`, not under `.github/`. To
activate it, copy the file into the **data-plane repo's** `.github/workflows/`:

```bash
cp deploy/workflows/spec-generator.yml \
   /path/to/data-plane-repo/.github/workflows/spec-generator.yml
# Then commit + push that file in the data-plane repo.
```

No fork of the agent code is needed — the workflow installs the agent via
`pip install` from this repo's `${{ vars.AGENT_REF || 'main' }}` ref. The
same `AGENT_REF` variable the Implementation Agent reads is reused, so a
single ref bump flips both agents in lockstep.

## 4. Secrets and variables

The SHADOW rollout needs the same OpenCode credentials the Implementation
Agent already has. The Slack URL is optional. No bot token, no App ID — those
ship in Tier 2.

### Repo variables (data-plane repo Settings → Variables)

| Name | Required for Tier 1 | Value |
|---|---|---|
| `OPENCODE_BASE_URL` | yes | `https://odoo-saas-opencode.fly.dev` |
| `AGENT_REF` | optional | A git ref pinning the installed agent (defaults to `main`). |
| `AGENTS_ENABLED` | yes | `true` (the kill switch — `false` makes the agent a no-op). |
| `SPEC_GEN_ROLLOUT_STAGE` | yes | `shadow` for Tier 1. `fixtures`/`opt_in`/`default_on` in Tier 2+. |
| `SPEC_GEN_ROLLOUT_FIXTURES` | optional | Comma-separated issue numbers for the `fixtures` stage. Unused in SHADOW. |
| `SPEC_GEN_ROLLOUT_OPT_IN` | optional | Comma-separated issue numbers for the `opt_in` stage. Unused in SHADOW. |

### Repo secrets (data-plane repo Settings → Secrets)

| Name | Required for Tier 1 | Value |
|---|---|---|
| `OPENCODE_SERVER_PASSWORD` | yes | The headless-OpenCode HTTP-Basic password (shared with the Implementation Agent). |
| `SLACK_WEBHOOK_URL` | optional | The escalation Slack webhook. SHADOW suppresses all Slack calls; set this so Tier 2 is one less variable to wire. |

## 5. Verify SHADOW with a fixture event

The smoke test reproduces what GitHub Actions does, locally and offline.

```bash
cd /Volumes/SATECHI2TB/userfolder/Odoo-saas-agents
source .venv-spec-gen/bin/activate

# Build a fixture issue payload.
cat > /tmp/fixture-issue.json <<'JSON'
{
  "action": "opened",
  "issue": {
    "number": 9999,
    "title": "Add CSV export to /sale/orders",
    "body": "We want a download-as-CSV button on the sale orders list.",
    "user": {"login": "alice"},
    "labels": [{"name": "feature-request"}]
  },
  "repository": {"full_name": "remcaro-rgb/GoliattCo-odoo-custom"}
}
JSON

# Run the agent in SHADOW against the fixture. OPENCODE_BASE_URL pointing at
# the real Fly app means the LLM call DOES go out — it's the comments and
# labels that stay drafted-but-unsent.
GITHUB_EVENT_NAME=issues \
GITHUB_EVENT_PATH=/tmp/fixture-issue.json \
DATA_PLANE_REPO=remcaro-rgb/GoliattCo-odoo-custom \
ROLLOUT_STAGE=shadow \
OPENCODE_BASE_URL=https://odoo-saas-opencode.fly.dev \
OPENCODE_SERVER_PASSWORD=<the password> \
python -m agents.spec_generator | jq -c .
```

You should see structured-JSON log records on stdout including:
- `rollout-decision` with `decision="RolloutDecision.SHADOW"`.
- `outcome` with `status="drafted"` and `shadow=true`.
- One or more `shadow-write` records — these are the would-be writes
  (`kind=issue_comment` for the bot's summary, `kind=issue_label` for
  `spec-drafted` + `awaiting-reporter-confirm`).

To force the offline path (no Fly call), point `OPENCODE_BASE_URL` at an
unreachable host: the agent will escalate the issue with `skip_reason=
EMPTY_DRAFT` and still exit 0.

## 6. What the agent will NOT do in Tier 1

- It does not open a PR. The drafter records what the PR would contain;
  the workspace push lands in Tier 2.
- It does not respond to reporter comments. `issue_comment` webhooks are
  routed but currently return `status=skipped` (the refiner is Tier 2).
- It does not attempt bug reproductions. A `bug`-classified intake is
  escalated to `needs-human` (Tier 4 ships the agentlab repro path).
- It does not auto-confirm. The 24h-silence sweep is Tier 3.
- It does not detect duplicates. Tier 5 introduces pgvector dup search.

## 7. The Tier 2 punch-list (what's missing for ACT)

Tier 1 is a faithful SHADOW. To turn the agent loose for real you need:

1. **Provision the `spec-generator-bot` GitHub App** — separate from
   `implementation-bot` so write scopes are independently revocable. Follow the
   same 30-minute manual procedure as `TIER-1-RUNBOOK.md` §5, substituting
   `spec-generator-bot` everywhere. Install on the data-plane repo.
2. **Add repo variables / secrets**:
   - `SPEC_GENERATOR_BOT_APP_ID` (variable, numeric App ID).
   - `SPEC_GENERATOR_BOT_PRIVATE_KEY` (secret, the App's `.pem`).
3. **Uncomment the `Mint a token`** step in `spec-generator.yml` (already
   stubbed with the right inputs).
4. **Wire workspace push** for the drafted spec PR — `pushback.py` analog. The
   drafter already produces a `DraftedSpec.branch` and `path`; the missing
   piece is staging the body to disk + a `git push` via the bot token.
5. **Add the iterate workflow** — `spec-generator-iterate.yml`, fires on
   `issue_comment.created` for issues with an open `agent/spec-*` PR.
6. **Wire `refiner.py`** — Tier 2's reporter Q&A loop, re-enters the same
   OpenCode session and runs `/speckit.clarify`.
7. **Flip `SPEC_GEN_ROLLOUT_STAGE`** from `shadow` → `fixtures` for the first
   live drafts.

## 8. Open questions decided for Tier 1 (assumptions in code today)

The plan at `docs/superpowers/plans/2026-05-23-spec-generator-agent.md` §5
flagged eight open questions. Tier 1 makes the following calls:

- **Q1 (shared `opencode_client`):** option (a)-lite — cross-package import
  from `agents.implementation` instead of a full `agents/shared/` refactor.
  The full refactor is deferred to keep the live Implementation Agent
  undisturbed; cross-imports already deliver the "single source of truth"
  property.
- **Q2 (repo structure):** same repo (`Odoo-saas-agents/agents/spec_generator/`).
- **Q6 (confidence threshold):** `LOW_CONFIDENCE_NOTICE_THRESHOLD = 0.65` in
  `core.py`. Below the threshold the bot still drafts but appends a
  low-confidence notice so the reporter can `/reclassify`. Hardened in Tier
  2 once we have 100 real classifications to tune against.
- **Q7 (`/clarify` vs custom Q&A loop):** drive `/speckit.clarify` internally
  via `SpecKitFrontDriver.run_clarify` (signature locked in Tier 1 even
  though the loop ships in Tier 2).
- **Q8 (handoff trigger):** the Implementation Agent already fires on
  `pull_request.labeled` with `intent-confirmed`. Applying that label IS the
  handoff trigger — no separate webhook ping needed.

Q3 / Q4 / Q5 stay deferred (Tier 3 / 5 / 5 respectively).

## 9. Verification checklist (the Tier 1 exit criteria from the plan)

- [x] `python -m agents.spec_generator` exits 0 with `GITHUB_EVENT_NAME=issues
      GITHUB_EVENT_PATH=fixture.json ROLLOUT_STAGE=shadow`.
- [x] Unit tests clean (`pytest tests/unit` — 352 tests pass).
- [x] `ruff check agents/spec_generator/` clean.
- [x] `mypy agents/spec_generator/` clean.
- [ ] A real `issues.opened` event on a test repo produces a structured-JSON
      log trail showing the would-be branch, would-be commit, would-be PR
      title, and would-be comment body — and posts nothing. *(Needs §3 deploy
      to the data-plane repo first.)*
- [ ] The drafted spec passes `spec-quality.yml` checks. *(Same dependency —
      data-plane deploy + a `/speckit.specify` capable OpenCode session.)*
