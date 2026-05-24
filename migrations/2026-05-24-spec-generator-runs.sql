-- Spec Generator state table (Tier 3, per plan §3 / design spec §6).
--
-- DEPLOY: applied to the CONTROL-PLANE Postgres database (the Drizzle DB
-- the Odoo-control-plane Next.js app owns). See SECRETS.md in the data-plane
-- repo for connection details; the migration is plain SQL so it can be
-- applied via `psql` or pasted into Drizzle's migration folder.
--
-- The row is created when the Spec Generator drafts a spec, updated at each
-- phase transition (clarify, awaiting-confirm, intent-confirmed, completed),
-- and read by the dashboard for status + cost rollups.

CREATE TABLE IF NOT EXISTS spec_generator_runs (
    -- 1. Surrogate key.
    id                BIGSERIAL PRIMARY KEY,

    -- 2. Source issue (the GitHub issue number on the data-plane repo).
    issue_number      INTEGER NOT NULL,

    -- 3. Spec PR number — NULL until the agent successfully opens the PR
    --    (between the draft and the push, this is briefly empty).
    pr_number         INTEGER,

    -- 4. Routing kind decided by the classifier
    --    (feature | bug | config | user_error | sensitive).
    kind              TEXT NOT NULL,

    -- 5. Classifier confidence (0..1).
    confidence        NUMERIC(4, 3) NOT NULL,

    -- 6. Source — github_issue | chatbot | email — for funnel analytics.
    source            TEXT NOT NULL DEFAULT 'github_issue',

    -- 7. OpenCode session id — re-entered by the refiner on every iteration.
    --    Tier 2's JsonFileSessionStore is replaced by reading this column.
    opencode_session_id TEXT,

    -- 8. Spec branch + path on the data-plane repo.
    branch            TEXT NOT NULL,
    spec_path         TEXT NOT NULL,

    -- 9. Phase — drafted | awaiting_reporter_confirm | intent_confirmed |
    --    completed | escalated.
    phase             TEXT NOT NULL DEFAULT 'drafted',

    -- 10. Per-run cost (USD) for the Tier 6 spend cap. Updated by the cost
    --     module after every LLM call.
    cost_usd          NUMERIC(8, 4) NOT NULL DEFAULT 0,

    -- 11. Last reporter activity timestamp — driven by `issue_comment`
    --     webhooks. The sweep uses this to decide when 24h of silence has
    --     elapsed.
    last_reporter_activity_at TIMESTAMPTZ,

    -- 12. Lifecycle timestamps.
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 13. Free-form metadata — open_questions count, dup-suspect-of, etc.
    --     JSON keeps the schema agnostic to future Tier 5/6 metadata.
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Lookup by issue is the hot read path (every webhook resolves a row).
CREATE UNIQUE INDEX IF NOT EXISTS spec_generator_runs_issue_uq
    ON spec_generator_runs (issue_number);

-- The sweep filters by phase + last activity.
CREATE INDEX IF NOT EXISTS spec_generator_runs_phase_idx
    ON spec_generator_runs (phase, last_reporter_activity_at);

-- The dashboard aggregates by created_at + kind.
CREATE INDEX IF NOT EXISTS spec_generator_runs_created_at_idx
    ON spec_generator_runs (created_at DESC);

-- `updated_at` autoupdate trigger.
CREATE OR REPLACE FUNCTION set_spec_generator_runs_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS spec_generator_runs_set_updated_at ON spec_generator_runs;
CREATE TRIGGER spec_generator_runs_set_updated_at
    BEFORE UPDATE ON spec_generator_runs
    FOR EACH ROW
    EXECUTE FUNCTION set_spec_generator_runs_updated_at();
