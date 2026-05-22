# `deploy/` — deployment artifacts

Files here are **not active in this repo**. They are version-controlled next to
the agent code but get deployed elsewhere.

## `workflows/`

The three GitHub Actions workflows that trigger the Implementation Agent. They
must be copied into the **data-plane repo**'s `.github/workflows/` directory —
the repo that spec PRs open against — because a workflow only fires on events in
the repo it lives in.

They are deliberately kept out of *this* repo's own `.github/workflows/`:
`notify-reporter-on-human-commit.yml` triggers `on: push`, so it would otherwise
fire on every push to the agent repo itself.

See [`../docs/TIER-1-RUNBOOK.md`](../docs/TIER-1-RUNBOOK.md) for the full deploy
procedure — secrets, repo variables, and the `implementation-bot` account.
