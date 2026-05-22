# Tier 1 — Make it runnable · Runbook

Phases A–F built the Implementation Agent as a *library*: a tested orchestrator
brain (`agents/implementation/`) driven by `handle_webhook`. Nothing constructed
the real object graph or fed it a GitHub event. **Tier 1 is the runnable layer**
— the composition root, the entry point, and the trigger workflows that turn the
library into an agent that fires on a GitHub event.

## Status

**Built and wired — not yet deployed.** The composition root, the
`python -m agents.implementation` entry point, and the three trigger workflows
exist and are unit-tested + shadow-dry-run verified. To make Tier 1 *live* you
must do three things this code cannot do for you — provision the
`implementation-bot` account (§5), deploy the workflows into the data-plane repo
(§3), and set the secrets / variables (§4) — then run the canary (Tier 3).

## 1. What Tier 1 delivers

| Piece | File | Role |
|---|---|---|
| Composition root | `agents/implementation/app.py` | Reads the event, consults the rollout gate, builds the real clients, calls `handle_webhook`. |
| Entry point | `agents/implementation/__main__.py` | `python -m agents.implementation`. |
| Shadow client | `github_io.ShadowGitHubClient` | Real reads, recorded-but-unsent writes — the SHADOW stage. |
| Trigger workflows | `deploy/workflows/*.yml` | Three GitHub Actions workflows, one per event. |

The agent runs as an **ephemeral GitHub Action** — one process per event — not
as an always-on service. The only long-running service is OpenCode (`opencode/`,
Phase A). This keeps the design's "swap only the brain — keep GitHub-Actions
triggers" constraint.

## 2. How the agent runs

A workflow runs `python -m agents.implementation`. GitHub Actions hands every
step the event name and a JSON payload file via `$GITHUB_EVENT_NAME` /
`$GITHUB_EVENT_PATH`. `run()` (in `app.py`):

1. Loads the event. No event → exits 0 (nothing to do).
2. Builds `Rollout.from_env()` and decides **ACT / SHADOW / SKIP** for the target.
3. SKIP → exits 0. Otherwise builds `OpenCodeClient`, `GitWorkspace`,
   `SpecKitDriver`, the orchestrator, and a GitHub client (`GhCliClient` for ACT,
   `ShadowGitHubClient` for SHADOW), then calls `handle_webhook`.
4. Emits structured JSON log records (`observability.EventLog`) throughout.

Exit code: **0** for a clean run — a correct escalation included — and **1** only
for a missing-config abort or an unhandled error, so a red Action means a genuine
failure, not a routine escalation.

## 3. Deploy the trigger workflows

The three workflows in `deploy/workflows/` must be copied into the **data-plane
repo**'s `.github/workflows/` directory — the repo spec PRs open against — on its
**default branch**. A workflow only fires on events in the repo it lives in, and
`issue_comment` workflows always run from the default branch's copy.

```
cp deploy/workflows/implementation-intent-confirmed.yml      <data-plane-repo>/.github/workflows/
cp deploy/workflows/implementation-iterate.yml               <data-plane-repo>/.github/workflows/
cp deploy/workflows/notify-reporter-on-human-commit.yml      <data-plane-repo>/.github/workflows/
```

| Workflow | Trigger | Flow |
|---|---|---|
| `implementation-intent-confirmed.yml` | PR labeled `intent-confirmed` | implement |
| `implementation-iterate.yml` | new `issue_comment` on a PR | reporter iteration |
| `notify-reporter-on-human-commit.yml` | push to `agent/**` | human-commit ping |

They are kept out of *this* repo's `.github/workflows/` on purpose: the push
workflow triggers `on: push` and would otherwise fire on every push here.

## 4. Secrets & repo variables

Set these on the **data-plane repo** (Settings → Secrets and variables → Actions).

**Repo variables** (not sensitive):

| Variable | Example | Notes |
|---|---|---|
| `OPENCODE_BASE_URL` | `https://odoo-saas-opencode.fly.dev` | The headless OpenCode service. |
| `AGENTS_ENABLED` | `true` | Kill switch — set `false` to disable the agent. |
| `ROLLOUT_STAGE` | `shadow` | `shadow` / `fixtures` / `opt_in` / `default_on`. |
| `ROLLOUT_FIXTURES` | `123,124` | Comma-separated target ids — used by the `fixtures` stage. |
| `ROLLOUT_OPT_IN` | `acme/odoo` | Comma-separated target ids — used by the `opt_in` stage. |
| `GATE1_ENABLED` | `false` | Keep `false` until agentlab exists (Tier 2). |
| `AGENT_REF` | `main` | Git ref/tag of the agent package to install — pin a release tag for production. |

**Repo secrets** (sensitive):

| Secret | What |
|---|---|
| `OPENCODE_SERVER_PASSWORD` | The OpenCode server's HTTP Basic password. |
| `IMPLEMENTATION_BOT_TOKEN` | The `implementation-bot` PAT — see §5. |

The rollout `target` is the PR number (a push uses the branch). The `fixtures` /
`opt_in` sets match against that id; `shadow` and `default_on` ignore it.

> The OpenCode **container** also needs its own `GITHUB_TOKEN` *Fly secret* (for
> `provision_workspace`'s clone). That is set on the Fly app, not here — see
> `docs/PHASE-A.md`.

## 5. The `implementation-bot` service account — **you must do this**

The agent posts comments, applies labels, and (later) commits as a dedicated
identity. **Creating accounts is something an agent must never do on your behalf**
— this checklist is yours to complete.

1. **Create the account.** Create a GitHub *machine user* named
   `implementation-bot` (a normal GitHub account dedicated to the agent). A
   GitHub App also works — it pushes as `implementation-bot[bot]`, which the
   webhook adapter already handles. A machine user is simpler for `gh` CLI auth.
2. **Grant repo access.** Add `implementation-bot` to the data-plane repo as a
   collaborator with **Write** access (needed to comment and label).
3. **Create its token.** As the bot, create a fine-grained Personal Access Token
   scoped to the data-plane repo with **Pull requests: read & write** and
   **Issues: read & write** (comments + labels), plus **Contents: read**. Store
   it as the `IMPLEMENTATION_BOT_TOKEN` repo secret (§4).
4. **Set up signed commits** (design §10.1). Generate a GPG key for the bot, add
   the public key to its GitHub account, and provide the private key as a Fly
   secret on the OpenCode service so agent commits are GPG-signed. *(Needed once
   the agent commits to a branch — the plan-artifact commits and the
   implement→PR-branch push; see §7.)*
5. **Verify it is recognised.** `implementation-bot` is already in `_BOT_LOGINS`
   (`github_adapter.py`), so a push by the bot is correctly treated as the
   agent's own work, not a human commit.

The bot is **not required for a SHADOW dry run** (§6) — shadow posts nothing. It
*is* required before the rollout reaches the `fixtures` stage (the first ACT).

## 6. Shadow-mode dry run

A dry run proves the runnable layer is wired correctly without posting anything.

**Offline wiring proof** (no OpenCode, no GitHub — included in CI as `test_app.py`):

```bash
# no event in the environment -> emits a "no-event" record, exits 0
python -m agents.implementation
```

**Synthetic-event shadow run** (composition root + rollout + adapter):

```bash
echo '{"action":"created"}' > /tmp/event.json
GITHUB_EVENT_NAME=star GITHUB_EVENT_PATH=/tmp/event.json \
ROLLOUT_STAGE=shadow AGENTS_ENABLED=true DATA_PLANE_REPO=acme/odoo \
python -m agents.implementation
# emits rollout-decision (SHADOW) + outcome records, exits 0
```

**Live shadow run.** With `ROLLOUT_STAGE=shadow` set on the data-plane repo,
label a real spec PR `intent-confirmed`. The `implementation-intent-confirmed`
workflow runs the *full* implement flow — it drives OpenCode for real — but the
`ShadowGitHubClient` records the comment/label instead of posting them. Inspect
the Action log: the structured records show what *would* have been posted.

## 7. Known caveats & Tier-2 follow-ups

- **Checkout / branch strategy.** The workflows check out the PR head branch
  (`intent-confirmed`) or `gh pr checkout` the PR (`iterate`). The orchestrator's
  `GitWorkspace.checkout` uses `git checkout -B`; confirm the branch tip is
  present on the first live `fixtures` run.
- **Gate 1 is off.** `GATE1_ENABLED=false` until the agentlab Odoo build
  environment exists (Tier 2). Until then the coder runs Odoo-rule validation
  only — no build/test gate.
- **Notifier is unwired.** Slack escalation routes (`notifier.py`) are built but
  need a webhook secret (Tier 2). Escalations still post a GitHub comment + label.
- **implement → PR-branch push.** The coder drives OpenCode to write addon files
  in OpenCode's workspace; the path that pushes that result back onto the PR
  branch as a bot commit is not yet wired. **When it is wired it must be made
  shadow-aware** — consult the rollout decision before pushing, because
  `ShadowGitHubClient` only suppresses GitHub *API* writes (comments / labels),
  not a `git push`. Until then, the `commenter.py` replies that say "pushed the
  code" run ahead of reality. Verify before the `default_on` stage.
- **Cost cap not enforced at the entry point.** `cost.py` (`Budget`,
  `session_cost`) is built; wiring a durable per-PR spend cap into `run()` needs
  cross-run state (Tier 2/3).
