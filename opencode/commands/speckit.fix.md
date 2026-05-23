---
description: Implement a fix-brief — `$ARGUMENTS` IS the directive.
agent: build
---

## User Input

```text
$ARGUMENTS
```

The text above is a **fix-brief** (per `docs/2026-05-15-spec-driven-dev-plan.md`
§2.5). It is the **primary directive** for this command — there is no
`tasks.md` or `plan.md` to read; the brief itself names the change, where to
apply it, and the regression test that should catch it.

## What to do

1. **Read the brief carefully.** The structure is fixed:
   - `## 4. Root cause` — names the offending file (often with the exact
     `path:line`).
   - `## 5. Proposed fix` — the actual code change, often with a before/after
     code block.
   - `## 6. Regression test` — concrete test code that should be added.

2. **Read the target file(s)** the brief points at, using your `read` tool.
   Do NOT scan the whole repo — the brief tells you exactly which file(s).

3. **Apply the change described in §5** using your `edit` tool. Stay strictly
   within scope:
   - Do not refactor surrounding code.
   - Do not rename things the brief did not ask you to rename.
   - Do not add comments beyond what the brief explicitly asks for.
   - If §5's code block shows `before` and `after`, your edit should
     transform `before` into `after` verbatim where possible.

4. **Add the regression test from §6** if the brief includes one. Drop it in
   the addon's `tests/` directory (Odoo addons keep tests in
   `tests/test_*.py`). Create the file if it does not exist. Make sure the
   addon's `tests/__init__.py` imports the new test module so Odoo picks it up.

5. **Re-read the modified file** to confirm the change matches §5. If the
   brief specifies an exact literal (a key name, a value, an import order),
   the file must contain that literal.

## Constraints

- **Do not** create or edit files outside the addon directory the brief
  targets (its `custom-addons/<addon>/` tree).
- **Do not** touch `infra/`, `.github/workflows/`, the project `Dockerfile`,
  or `saas_tenant_gate/security/` — the project's guardrails (also enforced
  by the permission deny-list and the agent-guardrails CI check).
- **Ship your work yourself.** After editing, end your run with this exact
  bash block — the container's `origin` is already authenticated to push
  the PR head branch:

  ```bash
  cd /workspace \
    && git config user.email "impl-agent@goliatt.co" \
    && git config user.name "Implementation Agent" \
    && git add -A \
    && git diff --cached --quiet && echo "no changes" && exit 0 || true \
    && git commit -m "[impl-agent] <short summary matching the fix>" \
    && git push origin HEAD
  ```

  Adapt the commit message to the change (e.g.
  `[impl-agent] add maintainers field to club_faq manifest`). Do **not** open
  a new branch or push anywhere else — push only to the branch you are
  already on (the PR's head branch).

## Output

After completing the change, output a short structured summary, nothing else:

```
Changed: <relative path(s) of files you edited or created>
Why:     <one sentence linking back to §5 of the brief>
Test:    <path of the regression test you added, or "skipped — no test sketch in brief">
```

No commentary beyond that block. No clarifying questions. No alternative
plans. The brief is the spec; the edit is the implementation; the summary is
the proof. If anything in the brief is genuinely impossible to apply, say so
plainly in the `Why:` line and stop — do not guess.
