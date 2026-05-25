# Embeddings Shim — self-hosted sentence-transformers

Free, owned embedding service for the Spec Generator's dup-detection layer.
A small FastAPI app that wraps **`BAAI/bge-small-en-v1.5`** (384 dimensions,
top-quartile on the MTEB English benchmark for its size class). One Fly
machine, ~130 MB on-disk model, ~250 MB resident memory.

## Why self-host

- **Cost: ~$2/month** (one `shared-cpu-1x` Fly VM, auto-stop when idle) vs
  $0.05/month-but-scaling on OpenAI/Voyage.
- **No external API to rotate / get rate-limited by.**
- **Same Fly-everything pattern** as the agentlab shim — one less vendor
  to remember.

The quality gap vs `text-embedding-3-small` is small for the agent's use
case (cosine similarity between English issue titles + first ~500 chars):
both score in the 0.9+ range on near-duplicate intakes in the
plan §10 fixture set.

## Contract

```
POST  /embed
Headers:
  Authorization: Bearer <EMBEDDINGS_SHIM_TOKEN>
  Content-Type: application/json
Body:
  {"input": "text to embed"}
Response 200:
  {"embedding": [0.0123, -0.0456, ...]}     # length 384

GET   /healthz       (public)
GET   /              (public — service info)
GET   /docs          (FastAPI Swagger UI)
```

Locked by `agents/spec_generator/embedding.py:LocalEmbeddingClient` on the
agent side.

## Deploy

```bash
cd embeddings-shim
fly launch --name odoo-saas-embeddings-shim --region iad --no-deploy --copy-config
fly secrets set --app odoo-saas-embeddings-shim \
    EMBEDDINGS_SHIM_TOKEN="$(openssl rand -hex 32)"
fly deploy --app odoo-saas-embeddings-shim
# Grab the token for the data-plane wiring:
fly secrets list --app odoo-saas-embeddings-shim  # (token redacted in output)
```

## Model swap

If you ever want richer embeddings, edit `MODEL_NAME` in
`embeddings_shim/main.py`:

| Model | Dims | Size | Quality | Notes |
|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` (default) | 384 | 130 MB | High | English-only |
| `BAAI/bge-base-en-v1.5` | 768 | 440 MB | Higher | English-only; bump VM memory |
| `BAAI/bge-large-en-v1.5` | 1024 | 1.3 GB | Highest | English-only; bump VM memory to 4 GB |
| `intfloat/multilingual-e5-small` | 384 | 470 MB | Mid | Multilingual; same 384d migration |

**Important:** changing dims requires the matching `VECTOR(<dim>)` column
in `migrations/2026-05-24-spec-gen-embeddings.sql` AND `DEFAULT_DIMENSIONS`
in `agents/spec_generator/embedding.py`. The agent will refuse to upsert
on a dim mismatch (built-in defense).

## Local dev

```bash
pip install -r requirements.txt
export EMBEDDINGS_SHIM_TOKEN="local-dev"
uvicorn embeddings_shim.main:app --port 8080 --reload
# in another shell:
curl -sS http://127.0.0.1:8080/healthz | jq
curl -sS -X POST -H "Authorization: Bearer local-dev" \
    -H "Content-Type: application/json" \
    --data '{"input": "hello world"}' \
    http://127.0.0.1:8080/embed | jq '.embedding | length'
# Expected: 384
```

First request after a cold start takes a few seconds (model load); all
subsequent requests are <50 ms on shared-cpu.
