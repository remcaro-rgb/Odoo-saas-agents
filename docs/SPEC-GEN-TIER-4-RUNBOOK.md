# Spec Generator Tier 4 — Agentlab Playwright Shim · Runbook

Tier 4 unlocks the bug-flow leg of the Spec Generator. A reporter files an
issue labeled `bug` → the agent calls a new Fly service
(`odoo-saas-odoo-agentlab-shim`) → the shim runs Playwright against the
existing agentlab Odoo tenant → the agent drafts a fix-brief if the bug
reproduces, or routes to security-leads / asks for more info if not.

## Status

**Code shipped, deployment pending.** The agent side (`HttpShimAgentlabClient`
in `agents/spec_generator/repro.py`) has been live since the Tier-1 ship; the
composition root now constructs it from env (see PR ref below). The shim
service itself lives in this repo at `agentlab-shim/` but is **not yet
deployed**. This runbook walks the one-time deployment.

## What Tier 4 delivers (code-side, already in)

| Piece | File | Role |
|---|---|---|
| Shim service | `agentlab-shim/agentlab_shim/{main,runner}.py` | FastAPI + Playwright orchestrator |
| Dockerfile | `agentlab-shim/Dockerfile` | `mcr.microsoft.com/playwright/python:v1.42.0-jammy` base |
| Fly config | `agentlab-shim/fly.toml` | `shared-cpu-2x` / 2 GB / auto-stop |
| Agent client | `agents/spec_generator/repro.py:HttpShimAgentlabClient` | HTTP caller with 6 min timeout |
| Composition root | `agents/spec_generator/app.py:build_agentlab_client` | Constructs client from `AGENTLAB_SHIM_URL` + `AGENTLAB_SHIM_TOKEN` |
| Workflow env | `deploy/workflows/spec-generator{.yml,-iterate.yml}` | Forwards both secrets |

## 1. Deploy the shim service to Fly

One-time, ~15 min. Same Fly org/account that already runs
`odoo-saas-odoo-agentlab` so the cross-call latency stays single-digit ms.

```bash
cd /Volumes/SATECHI2TB/userfolder/Odoo-saas-agents/agentlab-shim

# 1. Confirm Fly CLI is logged in and which region the agentlab app uses
fly auth whoami
fly status --app odoo-saas-odoo-agentlab | grep -i region

# 2. Edit fly.toml's `primary_region` line if it differs (current: "iad").

# 3. Launch — `fly launch` reads fly.toml, builds the Dockerfile,
#    and registers the app. Reject postgres / redis offerings; this
#    service has no database.
fly launch --name odoo-saas-odoo-agentlab-shim --no-deploy --copy-config

# 4. Set secrets (one shared bearer token; same value goes to the
#    data-plane repo in step 2).
SHIM_TOKEN=$(openssl rand -hex 32)
fly secrets set --app odoo-saas-odoo-agentlab-shim \
    AGENTLAB_SHIM_TOKEN="$SHIM_TOKEN" \
    AGENTLAB_BASE_URL="https://odoo-saas-odoo-agentlab.fly.dev"

# Save the token for step 2 — don't lose it.
echo "$SHIM_TOKEN" > ~/secrets/agentlab-shim-token.txt
chmod 600 ~/secrets/agentlab-shim-token.txt

# 5. Deploy
fly deploy --app odoo-saas-odoo-agentlab-shim

# 6. Verify
curl -sS https://odoo-saas-odoo-agentlab-shim.fly.dev/healthz | jq
# Expected: {"status":"ok","auth_configured":true}

# 7. Smoke-test /repro with the token
curl -sS -X POST \
    -H "Authorization: Bearer $SHIM_TOKEN" \
    -H "Content-Type: application/json" \
    --data '{"issue":1,"title":"smoke","body":"It is broken","attachments":[],"reporter":"runbook"}' \
    https://odoo-saas-odoo-agentlab-shim.fly.dev/repro | jq
# Expected: outcome=needs_repro_info (the body has no structured steps —
# the pre-flight short-circuits before any Playwright call)
```

## 2. Wire the data-plane secrets

```bash
# Token from step 1.4
SHIM_TOKEN=$(cat ~/secrets/agentlab-shim-token.txt)

gh secret set AGENTLAB_SHIM_URL --repo GoliattCo/odoo-custom \
    --body "https://odoo-saas-odoo-agentlab-shim.fly.dev"
gh secret set AGENTLAB_SHIM_TOKEN --repo GoliattCo/odoo-custom \
    --body "$SHIM_TOKEN"

# Verify
gh secret list --repo GoliattCo/odoo-custom | grep AGENTLAB
```

After that, the next bug-labeled issue triggers the spec-generator
workflow, which sees `AGENTLAB_SHIM_URL` + `AGENTLAB_SHIM_TOKEN` and builds
the real client.

## 3. Live verification (3 fixture bugs)

Open three deliberately-shaped bug issues against `ROLLOUT_FIXTURES`:

```bash
DPR=GoliattCo/odoo-custom

# 3.1 — confirmed-bug case
gh issue create --repo "$DPR" --title "[bug-canary-confirmed] PDF export 500" \
    --label bug \
    --body $'Steps:\n1. goto: /web/login\n2. fill: input[name=login] = admin\n3. fill: input[name=password] = admin\n4. click: button[type=submit]\n5. goto: /odoo/contacts/1\n6. click: button[name=action_print_pdf]\n\nExpected: PDF downloads\nActual: 500 page'

# 3.2 — needs-repro-info case
gh issue create --repo "$DPR" --title "[bug-canary-vague] it broke" \
    --label bug \
    --body "Sometimes things don't work. Please look."

# 3.3 — needs-fixture case
gh issue create --repo "$DPR" --title "[bug-canary-fixture] Acme Corp invoices" \
    --label bug \
    --body $'Steps:\n1. goto: /odoo/invoices\n2. tenant id 42 shows wrong total\n\nExpected: 1000\nActual: 999'
```

Add each issue number to `SPEC_GEN_ROLLOUT_FIXTURES` so the agent ACTs on
them. Then watch:

```bash
gh run list --repo "$DPR" --workflow="Spec Generator Agent" --limit 3
```

**Pass criteria** (Tier 4 exit from plan §3):
- All three classified correctly (`repro_confirmed` / `needs_repro_info` /
  `needs_fixture`).
- The `repro_confirmed` case produces a fix-brief PR including Playwright
  log tail + at least one screenshot.
- No agentlab tenant left running past the repro window (the shim's
  context cleanup is in `runner.execute_steps`).

## 4. Token rotation

```bash
# Generate new token
NEW=$(openssl rand -hex 32)

# Rotate Fly side first (so any in-flight requests still authenticate)
fly secrets set --app odoo-saas-odoo-agentlab-shim AGENTLAB_SHIM_TOKEN="$NEW"

# Then update GitHub secret (next workflow run picks it up)
echo "$NEW" | gh secret set AGENTLAB_SHIM_TOKEN --repo GoliattCo/odoo-custom

# Confirm
fly secrets list --app odoo-saas-odoo-agentlab-shim | grep AGENTLAB_SHIM_TOKEN
gh secret list --repo GoliattCo/odoo-custom | grep AGENTLAB_SHIM_TOKEN
```

No downtime if both sides land within the same minute.

## 5. Observability

The shim itself does not emit Axiom records (it's stateless and lives
behind the agent's call). Every agent run that calls it logs:

- `event=note text="bug intake -> shim outcome=<outcome>"`
- The fix-brief PR's body (when `repro_confirmed`) includes the shim's
  `logs` (last 4 KB) and base64 screenshots.

To debug the shim itself: `fly logs --app odoo-saas-odoo-agentlab-shim`.

## 6. Failure-mode runbook

| Symptom | Likely cause | Recovery |
|---|---|---|
| Agent emits `outcome=agentlab_unavailable shim unreachable` | Fly app stopped (auto-stop after idle) | First request wakes it; subsequent calls are warm. Acceptable. |
| Every shim call returns 401 | Token drift between Fly and GitHub secrets | Rotate per §4 |
| Every shim call returns 503 | `AGENTLAB_SHIM_TOKEN` Fly secret is unset | `fly secrets set AGENTLAB_SHIM_TOKEN=...` |
| Reproduction hangs / times out at 6 min | Playwright stuck on a navigation; the agentlab Odoo tenant is slow | Restart the agentlab Odoo app: `fly apps restart odoo-saas-odoo-agentlab` |
| Repro outcome always says `needs_repro_info` even with good steps | The reporter is using a list format the parser doesn't recognise | Check `runner._GOTO` etc.; accept `1. goto:` / `- goto:` / `goto:` |
| OOM at random | `shared-cpu-2x` 2 GB is the floor; multi-step Odoo navigations spike | Scale up the Fly VM size in `fly.toml` (`shared-cpu-4x`, 4 GB) and `fly deploy` |

## 7. Out of scope (still)

- Concurrent reproductions on the same agentlab tenant — Tier 4 serializes
  them via Fly's hard concurrency limit (`http_service.concurrency.hard_limit=8`).
- Cross-agentlab dataset isolation — the shim runs against the live
  agentlab; if you need per-bug snapshots, ship Tier 4b (a separate
  per-request tenant snapshot — design's §13 Q5 listed this as deferrable).
- Authenticated screenshots upload to durable storage — the shim returns
  inline base64. Acceptable up to ~5 MB total per response; for richer
  flows, post-process and upload to S3 / Vercel Blob and replace with URLs.
