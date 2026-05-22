# Headless OpenCode service for the Implementation Agent.
# Runs as a LONG-RUNNING service (plan decision 5) — see fly.toml.
#
# Versions are pinned (open question 5 — OpenCode pin & fork policy):
# bump them deliberately, behind the smoke test, treating a bump as a gated change.
FROM node:24-slim

ARG OPENCODE_VERSION=1.15.7
ARG AISDK_OPENAI_COMPAT_VERSION=2.0.47

# System packages: git (repo checkouts + OpenCode's diff/snapshots), jq (the
# Spec-Kit scripts' JSON output), ca-certificates (HTTPS), util-linux (for
# `setpriv`, used by the entrypoint to drop privileges).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git jq ca-certificates util-linux \
 && rm -rf /var/lib/apt/lists/*

# OpenCode CLI + the AI SDK provider package the OpenCode Go custom provider uses.
RUN npm install -g \
      "opencode-ai@${OPENCODE_VERSION}" \
      "@ai-sdk/openai-compatible@${AISDK_OPENAI_COMPAT_VERSION}" \
 && npm cache clean --force

# Unprivileged service account (task #57). The agent's bash/edit tools run as
# this user — never root — so a compromised or prompt-injected agent cannot
# modify system files, the OpenCode install, or its own permission config.
RUN useradd --create-home --uid 10001 --shell /bin/bash opencode

# Global OpenCode config: providers + permission deny-list + the Odoo coding
# rules. Baked root-owned and left read-only to `opencode`, so the agent cannot
# rewrite its own guardrails. Resolved via XDG_CONFIG_HOME (set below).
RUN mkdir -p /home/opencode/.config/opencode
COPY opencode.json /home/opencode/.config/opencode/opencode.json
COPY AGENTS.md     /home/opencode/.config/opencode/AGENTS.md
# Spec-Kit workflow exposed as OpenCode custom commands (speckit.plan, ...).
COPY commands/     /home/opencode/.config/opencode/commands/

# Phase-B validation fixture, quarantined under /opt (NOT /workspace): a frozen
# .specify/ scaffold + a sample spec. The Phase-B validation script copies it
# into a session workspace to exercise /speckit.plan -> tasks -> analyze live.
# Production runs do not touch it.
COPY phaseb-fixture/ /opt/phaseb-fixture/
RUN chmod -R +x /opt/phaseb-fixture/.specify/scripts/bash

# Session storage on a durable Fly volume (plan §4.2 — sessions must outlive a
# restart). HOME + XDG_* are pinned so OpenCode resolves config/state the same
# regardless of the runtime uid (the entrypoint changes uid via setpriv).
ENV HOME=/home/opencode \
    XDG_CONFIG_HOME=/home/opencode/.config \
    XDG_DATA_HOME=/data \
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

# Hand the agent's working tree to the unprivileged user. The config dir under
# /home/opencode/.config stays root-owned (above) so it is read-only to the agent.
RUN chown -R opencode:opencode /workspace

# entrypoint.sh fixes the /data volume's ownership (volumes mount root-owned)
# then drops root -> opencode before exec'ing the server. Bind to 0.0.0.0 so the
# orchestrator can reach it; HTTP Basic auth via the OPENCODE_SERVER_PASSWORD
# Fly secret (never baked in).
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
