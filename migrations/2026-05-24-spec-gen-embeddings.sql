-- Spec Generator dup-detection index (Tier 5, plan §3 / design §5.1.5).
--
-- DEPLOY: applied to the CONTROL-PLANE Postgres database via the data-plane
-- repo's `apply-control-plane-ddl` workflow (drop into infra/sql/ and trigger
-- the workflow). Idempotent — uses CREATE EXTENSION/TABLE/INDEX IF NOT EXISTS.
--
-- One row per indexed document. The ingest cron walks
-- docs/superpowers/specs/**/*.md plus currently-open GitHub issues, embeds
-- the title + first ~500 chars of body via the self-hosted embeddings shim
-- (`BAAI/bge-small-en-v1.5`, 384 dimensions), and upserts. The agent's
-- composition root can also fall back to OpenAI's `text-embedding-3-small`
-- but that ALSO has to return 384-d vectors (use OpenAI's `dimensions: 384`
-- request param to downscale).
--
-- Read path: the agent (PgvectorKnowledgeBase) computes the embedding for a
-- new intake, then runs `ORDER BY embedding <=> $query ASC LIMIT 3` (cosine
-- distance, lower is more similar). Plan §3 threshold is cosine ≥ 0.85 to
-- flag a duplicate — i.e. distance ≤ 0.15.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS spec_gen_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    -- 'spec' (a merged design / fix-brief) | 'open_issue' (a GitHub issue
    -- still in the triage funnel). Per plan §5 Q4: closed-rejected specs
    -- and closed-as-stale issues are deliberately NOT indexed.
    kind        TEXT NOT NULL,
    -- Stable canonical ref:
    --   kind=spec       -> "docs/superpowers/specs/<file>.md"
    --   kind=open_issue -> "<repo>#<issue-number>"
    ref         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    -- The string we actually embed — useful for debugging "why did this
    -- score this way" without re-fetching the source document.
    embed_text  TEXT NOT NULL,
    embedding   VECTOR(384) NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cosine distance index. ivfflat is fast to build + query; `lists = 100` is
-- the sweet spot for ~10k rows. If we exceed 100k rows, retune `lists` to
-- roughly sqrt(N) and REINDEX.
CREATE INDEX IF NOT EXISTS spec_gen_embeddings_embedding_idx
    ON spec_gen_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- The ingest cron walks by kind to refresh / prune.
CREATE INDEX IF NOT EXISTS spec_gen_embeddings_kind_refreshed_idx
    ON spec_gen_embeddings (kind, refreshed_at DESC);
