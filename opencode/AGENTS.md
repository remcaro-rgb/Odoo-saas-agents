# AGENTS.md — Odoo coding rules for the Implementation Agent

You are implementing changes to an **Odoo 19 multi-tenant SaaS**. All clients share one
image; tenants are isolated by `dbfilter`. Follow these rules on every task. They are the
*generative* half of Odoo correctness — the deterministic half is enforced by `coder.py`
(Phase C) and by CI. The hard guardrails below are also enforced by the OpenCode
permission deny-list and the `agent-guardrails` CI check; do not try to work around them.

## Module structure

- Custom addons live under `custom-addons/<addon>/`.
- Every addon has `__manifest__.py` and `__init__.py`. Subpackages (`models/`, `wizards/`)
  each need their own `__init__.py` that imports their modules.
- Standard layout: `models/`, `views/`, `security/`, `data/`, `tests/`, `static/`.

## `__manifest__.py`

- Always declare `name`, `version`, `depends`, `data`, `license`, `author`.
- `depends` MUST list every addon whose models/views you reference (`base`, `mail`, etc.).
- `data` MUST list every XML/CSV the addon ships, security files first.
- Bump `version` when the addon changes.

## Models

- Subclass `models.Model`; set `_name` (new) or `_inherit` (extend), and `_description`.
- Computed fields: always pair with `@api.depends(...)`. Validations: `@api.constrains(...)`.
- Never use a mutable default argument. Never shadow Odoo built-ins (`id`, `env`, `ids`).
- Prefer the ORM. If raw SQL is unavoidable, use `self.env.cr.execute(query, params)` with
  parameter binding — NEVER f-string / `%` / `.format()` SQL (injection risk).

## Security — non-negotiable

- EVERY new `models.Model` MUST have a matching line in `security/ir.model.access.csv`.
- Multi-tenant data needs record rules; never write code that can read across tenants.
- NEVER weaken, bypass, or edit `saas_tenant_gate/security/**` — it is the tenant boundary.

## Views

- View XML must be well-formed. Extend existing views via `<xpath>` inheritance, never by
  copy-paste. Keep `id`s namespaced to the addon.

## Tests

- Every behavioural change ships a test AND a negative-case test.
- Use `odoo.tests.common.TransactionCase`; tag with `@tagged(...)` as appropriate.
- Never reduce the existing test count.

## Hard guardrails

- Do NOT edit `infra/**`, `.github/workflows/**`, any `Dockerfile`, or
  `saas_tenant_gate/security/**`. (The permission layer will block these anyway.)
- Keep each change small: ≤ 400 added and ≤ 400 deleted lines per PR.
- Implement the confirmed spec — nothing beyond it. Do not rewrite the spec.
- Commit messages are prefixed `[impl-agent]`.

See `.specify/memory/constitution.md` for the full project constitution.
