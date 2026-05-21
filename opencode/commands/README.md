# OpenCode commands — the Spec-Kit workflow

OpenCode custom commands that expose Spec-Kit's workflow. The Dockerfile installs them
into `~/.config/opencode/commands/`, so `speckit_driver.py` can drive the workflow over
the OpenCode HTTP API — `run_command(session, "speckit.plan", …)` runs `speckit.plan.md`,
and so on for `tasks`, `analyze`, `implement` (plus `specify`, `clarify`, `checklist`,
`constitution`).

## Provenance

Generated from Spec-Kit's `speckit-*` skills (the `specify` CLI, `claude` integration):
each command body is the corresponding Spec-Kit skill's procedure **verbatim**, under an
OpenCode frontmatter (`description`, `agent`). Regenerate them when Spec-Kit is upgraded —
do not hand-edit the bodies.

## Resolves design-doc open question #1

OpenCode is not (yet) an official Spec-Kit integration target, so the Spec-Kit prompts
are installed here as OpenCode **custom commands** — the fallback the design doc
anticipated. The driver issues them by name; OpenCode runs the matching `*.md`.

## Runtime requirement

The Spec-Kit procedures reference the `.specify/` directory (its `scripts/`, `templates/`,
and `memory/constitution.md`). That directory must be present in the **workspace OpenCode
operates on** — wiring it into each per-spec checkout is a Phase-D task.
