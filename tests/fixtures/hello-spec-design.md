# Fixture spec — Phase A smoke test

**Status:** fixture (not a real feature)
**Purpose:** the trivial end-to-end target for the Phase-A smoke test.

## Goal

Create a single file, `hello.txt`, in the workspace root, containing exactly this line:

```
Hello from the Implementation Agent.
```

## Non-goals

- No Odoo addon, no models, no tests. This fixture exists only to prove the OpenCode
  harness can take a spec and produce a file edit end-to-end — nothing more.

## Acceptance

- `hello.txt` exists in the workspace root.
- Its contents are exactly the line above (a trailing newline is fine).
