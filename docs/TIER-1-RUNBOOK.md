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
| `GATE1_ENABLED` | `true` | Whether the coder loop runs Gate-1 (`ruff check`) before declaring success. |
| `GATE1_CHECK_SET` | `lint` | `lint` (ruff only — works in the Action runner) or `full` (build + tests — reserved for the agentlab-SSH runner, §7). |
| `AGENT_REF` | `main` | Git ref/tag of the agent package to install — pin a release tag for production. |
| `IMPLEMENTATION_BOT_APP_ID` | `1234567` | The `implementation-bot` GitHub App's App ID — see §5. |

**Repo secrets** (sensitive):

| Secret | What |
|---|---|
| `OPENCODE_SERVER_PASSWORD` | The OpenCode server's HTTP Basic password. |
| `IMPLEMENTATION_BOT_PRIVATE_KEY` | The `implementation-bot` GitHub App's private key — paste the full `.pem` contents (including the `-----BEGIN/END-----` lines). See §5. |
| `SLACK_WEBHOOK_URL` *(optional)* | Slack [incoming webhook](https://api.slack.com/messaging/webhooks) URL. When set, the agent pages `#devops-implementations` on escalations. Unset = no Slack (the agent uses a `FakeNotifier`). SHADOW stage always uses the fake even if set. |

The rollout `target` is the PR number (a push uses the branch). The `fixtures` /
`opt_in` sets match against that id; `shadow` and `default_on` ignore it.

> The OpenCode **container** also needs its own `GITHUB_TOKEN` *Fly secret* (for
> `provision_workspace`'s clone). That is set on the Fly app, not here — see
> `docs/PHASE-A.md`.

## 5. The `implementation-bot` GitHub App — **you must do this**

The agent posts comments, applies labels, and (later) commits as a dedicated
GitHub identity. **Creating accounts and registering apps is something an agent
must never do on your behalf** — this checklist is yours to complete.

1. **Register the App.** Go to `https://github.com/settings/apps/new`
   (personal-owned data-plane repo) or
   `https://github.com/organizations/<org>/settings/apps/new` (org-owned).
   Name it `implementation-bot`. Set **Webhook → Active: OFF** (we use Actions
   triggers, not App webhooks). **Where can this GitHub App be installed?** →
   **Only on this account.**
2. **Set repository permissions:** Metadata: Read · Pull requests: Read & write
   · Issues: Read & write · Contents: **Read & write** (the implement→push-back
   pushes commits as the App; the App without Contents: Read & write cannot
   push and ACT-mode runs will fail at the push step). No organization
   permissions, no event subscriptions.
3. **Generate a private key.** App settings page → **Generate a private key** →
   save the downloaded `.pem` file (shown only once).
4. **Note the App ID** at the top of the settings page (a number, e.g.
   `1234567`).
5. **Install the App on the data-plane repo.** Left sidebar of the App page →
   **Install App** → **Install** → **Only select repositories** → pick the
   data-plane repo.
6. **Store the credentials** on the data-plane repo (§4): the App ID as the
   `IMPLEMENTATION_BOT_APP_ID` repo *variable*, and the full PEM contents as the
   `IMPLEMENTATION_BOT_PRIVATE_KEY` repo *secret*. The workflows mint a
   short-lived (1 h) installation token at runtime via
   `actions/create-github-app-token@v1`.
7. **Signed commits** (design §10.1). GitHub Apps that commit via the REST API
   get GitHub-signed "Verified" commits for free; committing via `git` from the
   OpenCode container does not (the App identity has no GPG key). Decide the
   commit path when implement→push-back is wired (§7); until then no commits
   reach GitHub from the agent anyway.
8. **Verify it is recognised.** The App pushes as `implementation-bot[bot]`;
   `_BOT_LOGINS` in `github_adapter.py` strips the `[bot]` suffix, so the bot's
   own pushes are correctly treated as non-human commits.

The App is **not required for a SHADOW dry run** (§6) — shadow posts nothing. It
*is* required before the rollout reaches the `fixtures` stage (the first ACT).

> **Alternative.** A GitHub *machine user* (a normal account with a fine-grained
> PAT) also works — simpler to set up; the trade-offs are per-account PAT
> rotation, coarser permissions, and a different signed-commits path. If you go
> that way, store the PAT as `IMPLEMENTATION_BOT_TOKEN` and replace the
> `Mint a token …` step in each workflow with
> `GH_TOKEN: ${{ secrets.IMPLEMENTATION_BOT_TOKEN }}` directly on the consuming
> steps.

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
- **Gate 1 — lint only.** `GATE1_ENABLED=true` + `GATE1_CHECK_SET=lint` runs
  `ruff check {addon}` after each implement attempt (a failure drives a
  corrective re-prompt with the frontier model — the lint signal the coder
  loop uses to self-correct). `GATE1_CHECK_SET=full` adds the build + tests
  checks (`odoo --stop-after-init -d <db> -i <module>`), but those need a
  running Odoo + Postgres; a vanilla GitHub Actions runner has neither, so
  `full` is reserved for the future **agentlab-SSH runner** — a `CheckRunner`
  that SSHes into the running agentlab Fly app (`odoo-saas-odoo-agentlab`,
  see `docs/superpowers/specs/2026-05-16-agentlab-environment-design.md`)
  and runs the checks against agentlab's daily-restored masked dataset.
  Until that runner exists, keep `GATE1_CHECK_SET=lint`.
- **Notifier is unwired.** Slack escalation routes (`notifier.py`) are built but
  need a webhook secret (Tier 2). Escalations still post a GitHub comment + label.
- **implement → PR-branch push.** *Wired* on two paths, both shadow-aware:
  - **Action path** (`pushback.py`) — after an `implemented` / `iterated`
    outcome, the Action fetches OpenCode's session diff, applies it to the
    data-plane checkout, commits as `implementation-bot[bot]`, and pushes to
    the PR head branch using the App's installation token. SHADOW logs
    `push.shadowed` and skips. Requires App **Contents: Read & write** (§5
    step 2) before any ACT.
  - **Container path** (`provisioning.py`) — the OpenCode container's own
    git can commit + push autonomously during `/implement` using the Fly
    `GITHUB_TOKEN` secret. In SHADOW, `provision_workspace(shadow=True)`
    rewrites `origin` to a token-less URL after the initial fetch, so any
    subsequent autonomous `git push` from the container fails 401 — closing
    the gap the FIXTURES-stage ACT smoke surfaced. Pass-through:
    `build_orchestrator(..., decision=decision)` → `Orchestrator(shadow=…)`
    → `_provision(shadow=…)` → `_provision_script(shadow=…)`.
- **Cost cap not enforced at the entry point.** `cost.py` (`Budget`,
  `session_cost`) is built; wiring a durable per-PR spend cap into `run()` needs
  cross-run state (Tier 2/3).
