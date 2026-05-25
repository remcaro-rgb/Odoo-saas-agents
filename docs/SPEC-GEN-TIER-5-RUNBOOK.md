# Spec Generator Tier 5 — Duplicate Detection · Runbook

Tier 5 adds duplicate detection to the feature flow. When a reporter opens a
new feature-request issue, the agent embeds the title + first ~500 chars of
the body via OpenAI `text-embedding-3-small` and queries a pgvector index
of (a) already-merged design specs / fix-briefs and (b) currently-open
GitHub issues. A cosine score ≥ 0.85 to the top hit makes the agent
prefix the spec PR title with `[possible-dup]` and prepend a callout to the
spec body linking the original. Sub-threshold candidates still surface as
suggestions (no title prefix) so the reporter can decide.

A daily cron walks the data-plane repo's `docs/superpowers/specs/**/*.md`
and open issues to keep the index fresh.

## Status

**Code shipped, deployment pending.** The agent already has the wiring
(`agents/spec_generator/{embedding,pgvector_kb,ingest}.py` + orchestrator
hooks in `core.py`). The migration SQL and the daily cron workflow live in
this repo. To go live you need to:

1. Apply the migration to the control-plane Postgres (§1).
2. Get an OpenAI API key + set it on the data-plane repo (§2).
3. Deploy the daily ingest workflow (§3).
4. Wait one ingest cycle + open a canary that should match an existing
   spec (§4) to verify the loop end-to-end.

The pre-deploy posture is **safe** — `build_knowledge_base` returns `None`
when either env var is unset, the orchestrator's `dup_detector` stays
`None`, and the feature flow skips dup detection entirely. There is no
half-on state.

## What Tier 5 delivers (code-side, already merged)

| Piece | File | Role |
|---|---|---|
| Migration SQL | `migrations/2026-05-24-spec-gen-embeddings.sql` | `vector` extension + `spec_gen_embeddings` table + ivfflat index |
| Embedding client | `agents/spec_generator/embedding.py` | `OpenAIEmbeddingClient` (urllib, retries 429/5xx) + `FakeEmbeddingClient` for tests |
| KB | `agents/spec_generator/pgvector_kb.py` | `PgvectorKnowledgeBase` — psycopg + cosine-distance query |
| Ingest job | `agents/spec_generator/ingest.py` | Walks specs + issues, upserts, prunes; `python -m agents.spec_generator.ingest` |
| Daily cron | `deploy/workflows/spec-generator-embed-ingest.yml` | Schedule `0 10 * * *` UTC + `workflow_dispatch` |
| Orchestrator wiring | `agents/spec_generator/core.py:_draft_feature` | Runs detector before drafting; threads callout + title prefix |
| Composition root | `agents/spec_generator/app.py:build_orchestrator` | Reads `OPENAI_API_KEY` + `CONTROL_PLANE_PG_DSN`; downgrades silently if either missing |

## 1. Apply the migration

Drop the SQL into `infra/sql/` on the data-plane repo and trigger the
`apply-control-plane-ddl` workflow (same pattern as the Tier 3 migration —
see `docs/SPEC-GEN-TIER-3-RUNBOOK.md` if it doesn't ring a bell).

```bash
cd /Volumes/SATECHI2TB/userfolder/odoo-custom
git checkout -b spec-gen/tier5-migration
cp /Volumes/SATECHI2TB/userfolder/Odoo-saas-agents/migrations/2026-05-24-spec-gen-embeddings.sql \
   infra/sql/
git add infra/sql/2026-05-24-spec-gen-embeddings.sql
git commit -m "Tier 5: spec_gen_embeddings table + pgvector extension"
git push -u origin spec-gen/tier5-migration
gh pr create --fill
# After merge:
gh workflow run apply-control-plane-ddl.yml \
    --repo GoliattCo/odoo-custom \
    -f only=2026-05-24-spec-gen-embeddings.sql
```

Verify the table landed:

```bash
psql "$CONTROL_PLANE_PG_DSN" -c "\\d spec_gen_embeddings"
psql "$CONTROL_PLANE_PG_DSN" -c "SELECT extname FROM pg_extension WHERE extname='vector';"
# Expected: vector | (one row)
```

## 2. Wire the OpenAI API key

You'll need an OpenAI API key with embeddings access (any tier — read-only
billing is fine; text-embedding-3-small is $0.02/M tokens, well under $1/mo
at the agent's volume).

```bash
gh secret set OPENAI_API_KEY --repo GoliattCo/odoo-custom \
    --body "sk-<your-key>"

# Verify
gh secret list --repo GoliattCo/odoo-custom | grep OPENAI
```

## 3. Deploy the ingest workflow

```bash
cp /Volumes/SATECHI2TB/userfolder/Odoo-saas-agents/deploy/workflows/spec-generator-embed-ingest.yml \
   /Volumes/SATECHI2TB/userfolder/odoo-custom/.github/workflows/

cd /Volumes/SATECHI2TB/userfolder/odoo-custom
git checkout -b spec-gen/tier5-ingest-workflow
git add .github/workflows/spec-generator-embed-ingest.yml
git commit -m "Tier 5: deploy embed-ingest daily cron"
git push -u origin spec-gen/tier5-ingest-workflow
gh pr create --fill
```

After merge, trigger it manually once to seed the index:

```bash
gh workflow run "Spec Generator - embedding ingest" --repo GoliattCo/odoo-custom
gh run list --repo GoliattCo/odoo-custom --workflow="Spec Generator - embedding ingest" --limit 1
```

The first run will index every existing spec + open issue. Watch the
Axiom dashboard for an `ingest-summary` record carrying the counts.

## 4. Canary verification

Open a deliberately-near-duplicate feature-request issue. Pick a topic
already covered by an existing spec (e.g. another CSV export, another
bulk-archive action) and re-phrase the steps.

```bash
DPR=GoliattCo/odoo-custom
DUP_ISSUE=$(gh issue create --repo "$DPR" \
    --title "[tier5-dup-canary] CSV export from /partners" \
    --label feature-request \
    --body "Please add a CSV download button on the contacts list view." \
  | grep -oE 'issues/[0-9]+' | head -1 | cut -d/ -f2)

# Add to fixtures so the agent ACTs.
CURRENT=$(gh variable list --repo "$DPR" | awk '/SPEC_GEN_ROLLOUT_FIXTURES/ {print $2}')
gh variable set SPEC_GEN_ROLLOUT_FIXTURES --repo "$DPR" --body "${CURRENT},${DUP_ISSUE}"

# Re-fire the workflow (the first one ran before fixtures was updated).
gh issue edit "$DUP_ISSUE" --repo "$DPR" --remove-label "feature-request"
sleep 2
gh issue edit "$DUP_ISSUE" --repo "$DPR" --add-label "feature-request"
```

**Pass criteria:**

- Agent run completes `success`.
- A spec PR is opened.
- **PR title starts with `[possible-dup]`.**
- **Spec body starts with a "Possible duplicates:" callout** listing the
  matching spec(s) with their cosine-similarity scores.
- Axiom record `dup-candidates` (logged from the orchestrator's notes)
  shows at least one candidate above 0.85.

## 5. Cost monitoring

`text-embedding-3-small` is $0.02 per million input tokens. At the agent's
operating volume (low tens of issues per week + a daily refresh of <100
docs), expected monthly cost is **under $1**. The Tier 6 weekly-spend cap
covers OpenAI calls too if a runaway loop ever lands.

A single agent run on a 500-char intake costs roughly
`(500 chars / 4 chars-per-token) * $0.02/M = $0.0025`. Negligible.

## 6. Failure modes

| Symptom | Likely cause | Recovery |
|---|---|---|
| Every agent run for a feature lacks the dup callout | `OPENAI_API_KEY` not set on data-plane | Set per §2 |
| Agent run emits `warning: PgvectorKnowledgeBase.query failed` | Postgres unreachable / pgvector extension not installed | Re-run §1 migration; check `CONTROL_PLANE_PG_DSN` is correct |
| Daily cron always returns `specs_indexed=0` | Workspace checkout didn't include `docs/superpowers/specs/` | `fetch-depth: 1` in the workflow is the right call — verify the dir exists in `main` |
| False positives (`[possible-dup]` on unrelated specs) | Threshold too generous; index has too-short embed_text | Raise `DUPLICATE_THRESHOLD` in `dup_detector.py` from 0.85 toward 0.90, or raise `EMBED_BODY_CHARS` from 500 toward 1000 |
| False negatives (real dup never flagged) | Title-only matching not enough; bodies are too short | Lower `DUPLICATE_THRESHOLD` or expand `EMBED_BODY_CHARS` |
| `OPENAI_API_KEY` rate-limited | Burst from cron + interactive runs | The client retries 429 with 1/2/4s backoff up to `max_retries=3`. If still failing, generate a separate ingest-only key with higher quota. |
| ivfflat index slow on first query | Cold cache after `apply-control-plane-ddl` | First query loads centroids; subsequent ones are fast. Acceptable. |

## 7. Tuning knobs

| Knob | Default | Where | When to touch |
|---|---|---|---|
| `DUPLICATE_THRESHOLD` | 0.85 | `dup_detector.py` | Tune from false-positive / false-negative ratio over the first 50 dup-flags |
| `TOP_K_CANDIDATES` | 3 | `dup_detector.py` | More = noisier callout; less = miss potential matches |
| `EMBED_BODY_CHARS` | 500 | `ingest.py` | Raise if dup detection feels too coarse (more chars = more nuance, more $) |
| `ivfflat lists` | 100 | migration SQL | Retune to `sqrt(N)` once the index exceeds ~10k rows |
| `text-embedding-3-small` | model | `embedding.py` | Switch to `text-embedding-3-large` for richer signal — requires a new migration to `VECTOR(3072)` |

## 8. Out of scope

- **Cross-tenant dup detection.** The index is single-tenant; if you ever
  multi-tenant the agent, partition by tenant on a new `tenant_id` column
  in `spec_gen_embeddings`.
- **Real-time dup detection on issue *edits*.** The agent fires only on
  `issues.opened` / `issues.labeled`; subsequent edits don't re-classify.
  Acceptable — the spec PR is the iteration locus.
- **Closed-rejected specs in the index.** Plan §5 Q4 explicitly excludes
  these. If a topic was considered and rejected, the agent should re-draft
  the next time someone files it — the human reviewer is the right gate,
  not stale "we said no" baggage.
- **Chatbot intake.** Tier 5b in the plan; deferred until the Support
  Triage Agent's outbound endpoint exists.
