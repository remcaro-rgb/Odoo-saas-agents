"""Axiom HTTP sink for `EventLog` (Phase 1.2 observability — replaces Better Stack).

The Spec Generator emits structured JSON records during every run. By default
those records go to stdout (caught by GitHub Actions log capture) and to the
in-memory `EventLog.records` list. With Axiom configured, they also batch-POST
to https://api.axiom.co/v1/datasets/<dataset>/ingest at the end of the run.

Design decisions:

- **Batch, don't per-record-flush.** A spec-generator run emits 10-50 records
  in 30s; one HTTP round-trip at the end is cheaper than 10-50 of them
  during the run and doesn't push the workflow over the cold-start budget.
- **Best-effort.** A failed Axiom POST never crashes the agent — we log the
  failure to stderr and continue. The agent's stdout JSON remains the
  authoritative audit trail; Axiom is a derived dashboard view.
- **No external deps.** `urllib.request` instead of `httpx` so the sink works
  in any composition root without pulling extra wheels.

Activation: set `AXIOM_TOKEN` (secret) + `AXIOM_DATASET` (variable) on the
workflow. Unset = stdout-only, no Axiom call ever.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

AXIOM_API_BASE = "https://api.axiom.co"


class AxiomSink:
    """Buffers `EventLog` lines and flushes them as one batch to Axiom.

    Each line is the JSON the agent already serialises for stdout — Axiom
    accepts NDJSON natively, so the sink concatenates with newlines and POSTs.
    A maximum buffer length protects against runaway loops (a single Tier-2
    iteration shouldn't emit > 1000 events; a higher cap is a bug).
    """

    MAX_RECORDS = 1000

    def __init__(
        self,
        *,
        token: str,
        dataset: str,
        base_url: str = AXIOM_API_BASE,
        http_open: Callable[..., Any] | None = None,
    ) -> None:
        self.token = token
        self.dataset = dataset
        self.base_url = base_url.rstrip("/")
        self.buffer: list[str] = []
        self._dropped = 0
        # Indirected for unit tests; defaults to urllib.request.urlopen.
        self._http_open = http_open or urllib.request.urlopen

    def __call__(self, line: str) -> None:
        """The `sink` callable shape `EventLog` expects."""
        if len(self.buffer) >= self.MAX_RECORDS:
            self._dropped += 1
            return
        self.buffer.append(line)

    def flush(self) -> bool:
        """POST the buffered records to Axiom. Returns True on success.

        A failure logs a warning to stderr and clears the buffer (we don't
        retry from a transient runner — let GitHub Actions retain the
        stdout copy as the audit fallback).
        """
        if not self.buffer:
            return True
        body = "\n".join(self.buffer).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/v1/datasets/{self.dataset}/ingest",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/x-ndjson",
                "User-Agent": "spec-generator-agent",
            },
        )
        try:
            with self._http_open(req, timeout=10.0) as resp:
                status = getattr(resp, "status", None)
        except urllib.error.URLError as exc:
            sys.stderr.write(
                f"axiom-sink: flush failed ({type(exc).__name__}: {exc}); "
                f"{len(self.buffer)} record(s) discarded "
                f"(GitHub Actions stdout retains the audit copy).\n"
            )
            self.buffer.clear()
            return False
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"axiom-sink: unexpected failure ({type(exc).__name__}: {exc}); "
                f"{len(self.buffer)} record(s) discarded.\n"
            )
            self.buffer.clear()
            return False
        if status is not None and (status < 200 or status >= 300):
            sys.stderr.write(
                f"axiom-sink: ingest returned HTTP {status}; "
                f"{len(self.buffer)} record(s) discarded.\n"
            )
            self.buffer.clear()
            return False
        if self._dropped:
            sys.stderr.write(
                f"axiom-sink: {self._dropped} record(s) dropped (buffer cap "
                f"{self.MAX_RECORDS} hit during run).\n"
            )
        self.buffer.clear()
        self._dropped = 0
        return True


def stdout_and_axiom(token: str, dataset: str) -> Callable[[str], None]:
    """A composite sink: forward every line to stdout AND buffer for Axiom.

    Returns the sink callable. The returned callable also exposes
    ``.axiom`` so the composition root can call ``.axiom.flush()`` at the
    end of a run.
    """
    axiom = AxiomSink(token=token, dataset=dataset)

    def _sink(line: str) -> None:
        # Stdout first — never lose the audit trail to a sink bug.
        print(line, file=sys.stdout, flush=True)
        axiom(line)

    _sink.axiom = axiom  # type: ignore[attr-defined]
    return _sink


def maybe_build_axiom_sink(
    env: Mapping[str, str] | None = None,
) -> Callable[[str], None] | None:
    """Construct a stdout+Axiom sink when env vars are present; else `None`.

    `EventLog` uses its default stdout-only sink when this returns `None`.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    token = source.get("AXIOM_TOKEN")
    dataset = source.get("AXIOM_DATASET")
    if not (token and dataset):
        return None
    return stdout_and_axiom(token=token, dataset=dataset)
