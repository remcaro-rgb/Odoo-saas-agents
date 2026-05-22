"""`python -m agents.implementation` — the Implementation Agent entry point.

A GitHub Actions workflow (see `deploy/workflows/`) runs this on a repo event;
it delegates to the composition root in `app.py`. See `docs/TIER-1-RUNBOOK.md`
for the deploy + run procedure.
"""

from .app import main

raise SystemExit(main())
