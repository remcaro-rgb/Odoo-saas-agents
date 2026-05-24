# Agentlab Playwright Shim

The HTTP service that turns a `POST /repro` from the Spec Generator agent into
a real Playwright reproduction attempt against a short-lived single-tenant Odoo
on `odoo-saas-odoo-agentlab.fly.dev`.

## Contract

```
POST  /repro
Headers:
  Authorization: Bearer <AGENTLAB_SHIM_TOKEN>
  Content-Type: application/json
Body:
  {
    "issue": 42,
    "title": "PDF export broken",
    "body": "Steps:\n1. ...\nExpected: ...\nActual: ...",
    "attachments": ["https://uploads/1.png"],
    "reporter": "alice"
  }
Response 200:
  {
    "outcome": "repro_confirmed" | "needs_repro_info" | "needs_fixture" | "agentlab_unavailable",
    "summary": "PDF export returned HTTP 500",
    "logs": "<trim to 4 KB tail>",
    "screenshots": ["<base64 PNG>", ...],
    "questions": ["..."]   // only for needs_repro_info
  }
```

The contract is locked by `agents/spec_generator/repro.py:HttpShimAgentlabClient`
in this repo; this service implements the server side.

## Auth

Static bearer token. The Spec Generator sends `AGENTLAB_SHIM_TOKEN`, the shim
verifies. Both sides read it from the same secret (`AGENTLAB_SHIM_TOKEN` on
the data-plane repo for the agent; `AGENTLAB_SHIM_TOKEN` as a Fly secret for
the shim). Token rotation: re-issue → update both Fly secret and the GitHub
secret → no downtime if you flip them within the same minute.

## Local dev

```bash
pip install -r requirements.txt
playwright install chromium --with-deps
export AGENTLAB_SHIM_TOKEN="local-dev"
uvicorn agentlab_shim.main:app --port 8080 --reload
# in another shell:
curl -sS http://127.0.0.1:8080/healthz
curl -sS -X POST -H "Authorization: Bearer local-dev" \
  -H "Content-Type: application/json" \
  --data '{"issue":1,"title":"smoke","body":"Steps:\n1. open /web\nExpected: dashboard\nActual: 500","attachments":[],"reporter":"local"}' \
  http://127.0.0.1:8080/repro | jq
```

## Deploy (one-time)

```bash
cd agentlab-shim
fly launch --name odoo-saas-odoo-agentlab-shim --region <same region as odoo-saas-odoo-agentlab>
fly secrets set --app odoo-saas-odoo-agentlab-shim \
  AGENTLAB_SHIM_TOKEN="$(openssl rand -hex 32)" \
  AGENTLAB_BASE_URL="https://odoo-saas-odoo-agentlab.fly.dev"
# Note the printed token — copy it into GoliattCo/odoo-custom secrets:
gh secret set AGENTLAB_SHIM_URL --repo GoliattCo/odoo-custom \
  --body "https://odoo-saas-odoo-agentlab-shim.fly.dev"
gh secret set AGENTLAB_SHIM_TOKEN --repo GoliattCo/odoo-custom \
  --body "<the same token you set above>"
```

After that, the next bug-labeled issue on `GoliattCo/odoo-custom` fires the
spec-generator workflow, which calls the shim, which calls Playwright against
the agentlab tenant, which returns one of the four outcomes.

## What the runner does

For each `/repro` request, the runner:

1. **Pre-flight classification** (cheap, no agentlab call):
   - body has no "Steps:" or numbered list → `needs_repro_info` with prompts.
   - body references a tenant id / customer name → `needs_fixture` (no agentlab
     call; the row is sanitized data the security-leads team must produce).
2. **Browser run** (when pre-flight passes):
   - launch Chromium in agentlab's incognito context with a 60s nav timeout.
   - navigate to `${AGENTLAB_BASE_URL}/web/login`, log in as `admin/admin`.
   - parse the reporter's `Steps:` block — best-effort:
     - lines starting with `goto: <url>` → `page.goto(url)`
     - `click: <selector>` → `page.click(selector)`
     - `fill: <selector> = <value>` → `page.fill(selector, value)`
     - `wait: <selector>` → `page.wait_for_selector(selector)`
     - everything else → recorded as a hint, not executed.
   - if the run completes without an exception AND the reporter's
     "Expected:" string is NOT present on the final page → `repro_confirmed`.
   - if the run completes AND "Expected:" string IS present → `needs_repro_info`
     with `"the expected outcome appeared; cannot reproduce the bug as
     described"`.
3. **Cleanup** — every navigation runs in a fresh `BrowserContext`; nothing
   persists across requests beyond the agentlab tenant's database state
   (which the impl agent's existing Gate 1 owns the lifecycle for).

The Playwright runner is **stateless** and **single-tenant** — one request,
one browser context. Concurrency is bounded by Fly's machine count, not by
in-memory pools.
