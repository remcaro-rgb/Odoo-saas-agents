"""`python -m agents.spec_generator` — the Spec Generator entry point.

A GitHub Actions workflow (see ``deploy/workflows/spec-generator.yml``) runs
this on an issue webhook; it delegates to the composition root in ``app.py``.
See ``docs/SPEC-GEN-TIER-1-RUNBOOK.md`` for the deploy + run procedure.
"""

from .app import main

raise SystemExit(main())
