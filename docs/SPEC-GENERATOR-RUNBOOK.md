# Spec Generator — Runbook (Tiers 1-6)

The Spec Generator is the **front half** of the SDD workflow — it converts a
free-form GitHub Issue (or chatbot conversation) into a structured design
spec or fix-brief PR, then iterates with the reporter until the spec lands
`intent-confirmed` and the Implementation Agent picks it up.

This runbook covers all six tiers shipped to the `spec-generator/tier1-shadow`
branch. The code is unit-tested + offline-smoke-verified; live verification
still depends on three external provisioning steps documented in §3 (the
`spec-generator-bot` App, the agentlab Playwright shim, the chatbot gateway).

## Status — what is live, what is staged

| Tier | Scope | Code | Tests | Live verification |
|---|---|---|---|---|
| 1 | SHADOW skeleton, draft-design-spec | shipped | shipped | needs data-plane deploy |
| 2 | refiner.py, push-back, iterate workflow | shipped | shipped | needs bot App provision |
| 3 | sweep + Postgres `spec_generator_runs` | shipped | shipped | needs migration apply |
| 4 | Bug repro on agentlab + fix-brief drafter | shipped | shipped | needs agentlab shim |
| 5 | Dup detection + chatbot webhook intake | shipped | shipped | needs pgvector + gateway |
| 6 | Prompt-injection deny-list + spend cap + dashboard | shipped | shipped | needs Better Stack import |

All 433 unit tests pass. ruff + mypy clean. The agent is fully runnable in
SHADOW today; flipping any individual tier to ACT requires the matching
infrastructure step in §3.

## 1. What the agent does

The orchestrator (`agents/spec_generator/core.py`) runs this pipeline on
every webhook:

```
Event
  -> Tier-6: prompt-injection scan (regex deny-list, fails closed)
     -> Tier-6: spend cap check ($50/week rolling)
        -> Tier-1: classifier (feature / bug / config / user_error / sensitive)
           |-- sensitive  -> route to security-leads, do not draft
           |-- feature    -> Tier 1 /speckit.specify -> DraftedSpec
           |-- bug        -> Tier 4 Reproducer -> draft_fix_brief if confirmed
           |-- config / user_error -> route to support inbox
  -> Tier-2: push DraftedSpec, open spec PR, persist OpenCode session id
  -> Tier-2: refiner — re-run /speckit.clarify on each reporter comment
  -> Tier-3: sweep — cron auto-confirm after 24h silence, advance label
```

`Tier-5 dup-detection` is wired alongside the classifier but disabled in
this drop (the production cron requires pgvector + the embeddings ingest
job — see §3 below). Chatbot intake is staged as
`deploy/workflows/spec-generator-webhook-inbound.yml`.

## 2. Modules at a glance

```
agents/spec_generator/
  __init__.py                 # package exports
  __main__.py                 # `python -m agents.spec_generator`
  app.py                      # composition root + GitHub Actions entry point
  events.py                   # normalized Event types
  github_adapter.py           # GitHub Issues webhook -> Event
  github_io.py                # IssueClient + handle_webhook + shadow client
  intake.py                   # Issue -> Intake (title/body/labels/attachments)
  classifier.py               # feature/bug/config/user_error/sensitive routing
  speckit_driver.py           # drives /speckit.specify · /clarify
  drafter.py                  # Intake -> DraftedSpec (design + fix-brief)
  commenter.py                # bot-voice comment rendering
  core.py                     # orchestrator + DraftResult
  refiner.py                  # Tier 2 reporter Q&A loop
  pushback.py                 # Tier 2 write-spec-and-push
  session_store.py            # PR -> OpenCode session id persistence
  sweep.py                    # Tier 3 auto-confirm sweeper
  repro.py                    # Tier 4 agentlab Playwright dispatcher
  dup_detector.py             # Tier 5 pgvector embedding search
  chatbot_intake.py           # Tier 5 chatbot gateway webhook
  prompt_injection.py         # Tier 6 adversarial scan + sanitiser
  cost.py                     # Tier 6 spend cap + ledger
```

```
deploy/workflows/
  spec-generator.yml                  # Tier 1+: issues.opened / issues.labeled
  spec-generator-iterate.yml          # Tier 2:  issue_comment.created
  spec-generator-sweep.yml            # Tier 3:  daily cron 09:00 UTC
  spec-generator-webhook-inbound.yml  # Tier 5:  chatbot repository_dispatch
deploy/dashboards/
  spec-generator-better-stack.json    # Tier 6:  dashboard + alerts
migrations/
  2026-05-24-spec-generator-runs.sql  # Tier 3:  spec_generator_runs schema
```

## 3. Deploy — what each tier needs

### Tier 1 (SHADOW only — minimum to install)

- Copy `deploy/workflows/spec-generator.yml` into the **data-plane repo**'s
  `.github/workflows/`.
- Repo variables (data-plane repo Settings → Variables):
  - `OPENCODE_BASE_URL` = `https://odoo-saas-opencode.fly.dev`
  - `AGENTS_ENABLED` = `true`
  - `SPEC_GEN_ROLLOUT_STAGE` = `shadow`
  - `AGENT_REF` (optional) = git ref pinning the installed agent.
- Repo secrets:
  - `OPENCODE_SERVER_PASSWORD` (shared with the Implementation Agent).
  - `SLACK_WEBHOOK_URL` (optional — SHADOW suppresses Slack anyway).

### Tier 2 — flip to ACT

1. **Provision the `spec-generator-bot` GitHub App** following the same
   30-minute procedure as the Implementation Agent's TIER-1-RUNBOOK §5,
   substituting `spec-generator-bot` everywhere. Required scopes:
   - `Contents`: **Read & write** (push the spec branch).
   - `Issues`: **Read & write** (post comments + add labels).
   - `Pull requests`: **Read & write** (open the spec PR).
2. Install the App on the data-plane repo.
3. Add to the data-plane repo:
   - Variable `SPEC_GENERATOR_BOT_APP_ID` = the App's numeric id.
   - Secret `SPEC_GENERATOR_BOT_PRIVATE_KEY` = the App's `.pem`.
4. Copy `deploy/workflows/spec-generator-iterate.yml` into
   `.github/workflows/`.
5. Flip `SPEC_GEN_ROLLOUT_STAGE` from `shadow` -> `fixtures` (canary against
   a small set of test issues) -> `opt_in` -> `default_on`.

### Tier 3 — auto-confirm sweep + Postgres state

1. Apply `migrations/2026-05-24-spec-generator-runs.sql` against the
   **control-plane** Postgres (the Drizzle DB the Odoo-control-plane
   Next.js app owns). The migration is idempotent (`IF NOT EXISTS`).
2. Copy `deploy/workflows/spec-generator-sweep.yml` into the data-plane
   repo's `.github/workflows/`.
3. The Tier-2 `JsonFileSessionStore` continues to work; Tier 3's Postgres
   binding is wired by setting `SPEC_GEN_SESSION_STORE=postgres` (deferred —
   the binding ships as a follow-up; the JSON store is fine for <=1k PRs/wk).

### Tier 4 — bug repro on agentlab

1. **Provision the agentlab Playwright shim** — an HTTP service the agent
   POSTs to with `{issue, title, body, attachments, reporter}` and gets
   back `{outcome, summary, logs, screenshots}`. Recommended deploy:
   alongside the `odoo-saas-odoo-agentlab` Fly app (the same one
   GATE1_CHECK_SET=full will use; design §10 §canary).
2. Configure on the data-plane repo:
   - Secret `AGENTLAB_SHIM_URL`.
   - Secret `AGENTLAB_SHIM_TOKEN` (HMAC shared with the shim).
3. The orchestrator picks up `HttpShimAgentlabClient` automatically once
   `AGENTLAB_SHIM_URL` is set. SHADOW continues to short-circuit before
   any shim call.

### Tier 5 — dup detection + chatbot intake

1. Stand up the pgvector index for `docs/superpowers/specs/**/*.md` +
   open issue bodies. Schema:
   ```sql
   CREATE TABLE spec_gen_embeddings (
     id BIGSERIAL PRIMARY KEY,
     kind TEXT NOT NULL,            -- spec | open_issue
     ref  TEXT NOT NULL,            -- spec path or issue URL
     title TEXT NOT NULL,
     embedding VECTOR(1536),
     refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   CREATE INDEX ON spec_gen_embeddings USING ivfflat (embedding vector_cosine_ops);
   ```
2. Ship the embeddings ingest cron — daily refresh, OpenAI / Voyage / etc.
   for the embedding model. The default `BagOfWordsKnowledgeBase` is for
   tests only; production wires `HttpKnowledgeBase` (sketched in the
   docstring of `dup_detector.py`).
3. Provision the chatbot gateway:
   - The gateway POSTs `repository_dispatch` events of type
     `spec-generator-chatbot-intake` to the data-plane repo, with HMAC
     verified against `secrets.SPEC_GENERATOR_CHATBOT_SECRET`.
   - Copy `deploy/workflows/spec-generator-webhook-inbound.yml`.

### Tier 6 — observability + cost cap

1. Import `deploy/dashboards/spec-generator-better-stack.json` into Better
   Stack — defines 9 panels + 3 alerts (classifier accuracy, repro failure
   rate, spend cap).
2. The prompt-injection deny-list and spend cap are wired into the
   orchestrator and active **regardless of tier** — they protect SHADOW
   too. The deny-list runs before any LLM call; the spend cap runs after
   the deny-list but before the classifier.
3. The kill switch is `AGENTS_ENABLED=false` (shared with the
   Implementation Agent). Sets `Rollout.decide` to `SKIP` for all events;
   `python -m agents.spec_generator` exits 0 with `event=skipped`.

## 4. How the agent runs

A workflow runs `python -m agents.spec_generator`. GitHub Actions hands the
step `$GITHUB_EVENT_NAME` + `$GITHUB_EVENT_PATH`. `app.run()`:

1. Loads the event. No event -> exits 0.
2. Builds `Rollout.from_env()` and decides ACT / SHADOW / SKIP.
3. SKIP -> exits 0. Otherwise builds the object graph and calls
   `handle_webhook`.
4. Records the outcome + every shadow-write as structured JSON via
   `EventLog`, tagged `agent_override=spec_generator` so the Better Stack
   drain filters cleanly against the Implementation Agent's records.

Exit code: **0** for any clean outcome (drafted, escalated, skipped).
**1** is reserved for missing-config aborts and unhandled exceptions.

## 5. Verify SHADOW with a fixture event

```bash
cd /Volumes/SATECHI2TB/userfolder/Odoo-saas-agents
source .venv-spec-gen/bin/activate

cat > /tmp/fixture-issue.json <<'JSON'
{
  "action": "opened",
  "issue": {
    "number": 9999,
    "title": "Login error",
    "body": "Please add this. I tried with password=hunter2 and got an error.",
    "user": {"login": "alice"},
    "labels": []
  },
  "repository": {"full_name": "remcaro-rgb/GoliattCo-odoo-custom"}
}
JSON

GITHUB_EVENT_NAME=issues \
GITHUB_EVENT_PATH=/tmp/fixture-issue.json \
DATA_PLANE_REPO=remcaro-rgb/GoliattCo-odoo-custom \
ROLLOUT_STAGE=shadow \
OPENCODE_BASE_URL=http://unreachable.invalid:9999 \
python -m agents.spec_generator | jq -c .
```

You should see:
- `rollout-decision` with `decision=RolloutDecision.SHADOW`.
- `outcome status=escalated skip_reason=sensitive_content` (the body
  contains a fake credential).
- `shadow-write kind=issue_comment` + `shadow-write kind=issue_label`
  showing the would-be writes.

Replace the body with a clean feature request to exercise the draft path
(needs `OPENCODE_SERVER_PASSWORD` for the live `/speckit.specify` call).

## 6. Operational runbook — failure modes

| Symptom | Detection | Recovery |
|---|---|---|
| Classifier mis-routes (bug as feature or vice versa) | dashboard panel "Classification distribution" diverges from manual audit | Reporter `/reclassify bug` or `/reclassify feature`; agent reruns the drafter. Track accuracy via 5% sampled audit. |
| Prompt-injection burst | `skip_reason:prompt_injection` rate panel spikes | Agent already refuses to draft; manually review the audit log, add new patterns to `prompt_injection._PATTERNS` if missed. |
| Spend cap reached | `weekly spend > 80% of cap` alert pages on-call | The agent refuses new drafts until rollover. Page on-call to investigate the burst; cap can be raised by adjusting `weekly_cap_usd` in the composition root. |
| OpenCode unreachable | `outcome status=escalated skip_reason=empty_draft` rate spikes | Fly app health; restart `opencode` service. Agent's failure mode is "escalate to human", not "crash". |
| Agentlab shim 5xx | `outcome` records show `agentlab_unavailable` summary | Fly app health on the `odoo-saas-odoo-agentlab` deployment. Agent escalates to `needs-human`. |
| Sweep auto-confirms a spec that wasn't ready | reporter `/reopen` within 7 days | Tier 3 sweep removes `intent-confirmed`, restores `awaiting-reporter-confirm`, posts a reopen comment. (Reopen path is documented but not yet wired — Tier 3 follow-up.) |
| chatbot gateway invalid HMAC | `chatbot-intake-rejected` log records | Rotate `SPEC_GENERATOR_CHATBOT_SECRET`, gateway-side. |
| Bot self-comment loop | `iterate` workflow re-fires on agent's own comment | Already guarded — `github_io._handle_issue_comment` short-circuits when `actor.removesuffix("[bot]") == "spec-generator-bot"`. |

## 7. Open questions decided in the implementation (vs the plan §5)

- **Q1 (`opencode_client` sharing):** cross-package import from
  `agents.implementation` (option a-lite). Full `agents/shared/` refactor
  deferred to avoid disturbing the live Implementation Agent.
- **Q2 (repo structure):** same repo, `Odoo-saas-agents/agents/spec_generator/`.
- **Q3 (auto-confirm opt-in):** not yet tenant-configurable — the Tier 3
  sweep is on for all PRs in `awaiting-reporter-confirm`. Adding the
  `auto_confirm_specs` tenant flag is a Tier 3 follow-up.
- **Q4 (dup-detector scope):** open issues + open spec PRs only. Closed-as-
  rejected specs are excluded so the agent doesn't resurrect stale "we said
  no" cases.
- **Q5 (chatbot upstream-label trust):** re-classify (defence in depth).
  The chatbot's `suggested_kind` becomes a `kind_hint`; the classifier
  still has the final say.
- **Q6 (confidence threshold):** 0.65 — below the threshold the bot still
  drafts but appends a `/reclassify` notice in the comment.
- **Q7 (`/clarify` vs custom Q&A):** drive `/speckit.clarify` internally
  via `SpecKitFrontDriver.run_clarify`; the reply text becomes
  `$ARGUMENTS`.
- **Q8 (intent-confirmed trigger):** the Implementation Agent already
  fires on `pull_request.labeled` with `intent-confirmed`. No separate
  webhook needed.

## 8. Verification checklist (per-tier exit criteria from plan §3)

**Tier 1** (code complete; 2 live items pending data-plane deploy)
- [x] `python -m agents.spec_generator` exits 0 on a fixture event in SHADOW.
- [x] `pytest tests/unit` — 433 tests pass.
- [x] `ruff check agents/spec_generator/` clean.
- [x] `mypy agents/spec_generator/` clean.
- [x] Offline SHADOW smoke produces a complete shadow-write audit trail.
- [ ] Live `issues.opened` on a deployed workflow produces the same trail.
- [ ] Drafted spec passes `spec-quality.yml` (needs `/speckit.specify` LLM call).

**Tier 2**
- [x] Refiner classifies `/confirm`, `/reclassify X`, free-text, noise.
- [x] PR pushback writes the spec, commits, pushes — idempotent on rerun.
- [x] Bot-self-comment loop is guarded.
- [ ] Manually labeling a real test-repo issue `feature-request` produces a real PR <10 min.
- [ ] Reporter `/confirm` advances label and fires the Implementation Agent's workflow.

**Tier 3**
- [x] Sweep auto-confirms silent PRs without open questions.
- [x] Sweep skips PRs with `[NEEDS CLARIFICATION]` markers.
- [x] Dry-run mode computes decisions but writes nothing.
- [ ] Cron runs cleanly nightly for 7 days.
- [ ] One real auto-confirm + one real `spec-refinement-needed` round-trip observed.

**Tier 4**
- [x] Pre-flight routes missing-steps to `needs-repro-info`.
- [x] Pre-flight routes customer-data references to `needs-fixture`.
- [x] Confirmed repro drafts a fix-brief that includes log tail + screenshots.
- [x] No LLM call on bug flow (`/speckit.specify` not invoked).
- [ ] 3 test bug issues classified correctly against the live agentlab shim.

**Tier 5**
- [x] Above-threshold dup query marks the new issue with `[possible-dup]`.
- [x] Chatbot payload HMAC verification rejects bad signatures.
- [x] `source:chatbot` label preserved through the orchestrator.
- [ ] 20 hand-crafted dup queries return correct top-1 against pgvector.
- [ ] Chatbot-sourced issue produces a spec with `source:chatbot` on the PR.

**Tier 6**
- [x] 8 prompt-injection categories detected; matched text never echoed.
- [x] `sanitise()` replaces matches with `[REDACTED:<category>]`.
- [x] Spend cap refuses new drafts above $50/week and at 80% warns.
- [x] Better Stack dashboard JSON imports cleanly (validated locally).
- [ ] Adversarial test set (50+ payloads) passes against a live deployment.
- [ ] Spend cap enforced under a 100-issue burst (load test).
