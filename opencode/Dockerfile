# Headless OpenCode service for the Implementation Agent.
# Runs as a LONG-RUNNING service (plan decision 5) — see fly.toml.
#
# Versions are pinned (open question 5 — OpenCode pin & fork policy):
# bump them deliberately, behind the smoke test, treating a bump as a gated change.
FROM node:24-slim

ARG OPENCODE_VERSION=1.15.7
ARG AISDK_OPENAI_COMPAT_VERSION=2.0.47

# System packages: git (repo checkouts + OpenCode's diff/snapshots), jq (the
# Spec-Kit scripts' JSON output), ca-certificates (HTTPS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git jq ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# OpenCode CLI + the AI SDK provider package the OpenCode Go custom provider uses.
RUN npm install -g \
      "opencode-ai@${OPENCODE_VERSION}" \
      "@ai-sdk/openai-compatible@${AISDK_OPENAI_COMPAT_VERSION}" \
 && npm cache clean --force

# Global OpenCode config baked into the image: providers + permission deny-list,
# plus the Odoo coding rules. ~/.config/opencode is OpenCode's global config dir.
RUN mkdir -p /root/.config/opencode
COPY opencode.json /root/.config/opencode/opencode.json
COPY AGENTS.md     /root/.config/opencode/AGENTS.md
# Spec-Kit workflow exposed as OpenCode custom commands (speckit.plan, ...).
COPY commands/     /root/.config/opencode/commands/

# Phase-B validation fixture, quarantined under /opt (NOT /workspace): a frozen
# .specify/ scaffold + a sample spec. The Phase-B validation script copies it
# into a session workspace to exercise /speckit.plan -> tasks -> analyze live.
# Production runs do not touch it.
COPY phaseb-fixture/ /opt/phaseb-fixture/
RUN chmod -R +x /opt/phaseb-fixture/.specify/scripts/bash

# Session storage on a durable Fly volume (plan §4.2 — sessions must outlive a
# restart). OpenCode 1.15.7 keeps the session DB under $XDG_DATA_HOME/opencode;
# XDG_STATE_HOME is pinned to /data too as insurance, since OpenCode has
# signalled intent to move state/log files under XDG_STATE_HOME.
ENV XDG_DATA_HOME=/data \
    XDG_STATE_HOME=/data \
    OPENCODE_LOG_LEVEL=debug
VOLUME ["/data"]

WORKDIR /workspace
EXPOSE 4096

# Phase-B validation only: pre-populate /workspace from the fixture so the
# validation drives /speckit.* with no agent-run setup turn. Remove for
# production — real per-spec checkouts are provisioned in Phase D.
RUN cp -rT /opt/phaseb-fixture/.specify /workspace/.specify \
 && cp -rT /opt/phaseb-fixture/specs /workspace/specs

# Bind to 0.0.0.0 so the orchestrator can reach it. The server is protected by
# HTTP Basic — set OPENCODE_SERVER_PASSWORD as a Fly secret (never bake it in).
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
