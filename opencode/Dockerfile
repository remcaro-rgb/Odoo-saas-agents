# Headless OpenCode service for the Implementation Agent.
# Runs as a LONG-RUNNING service (plan decision 5) — see fly.toml.
#
# Versions are pinned (open question 5 — OpenCode pin & fork policy):
# bump them deliberately, behind the smoke test, treating a bump as a gated change.
FROM node:24-slim

ARG OPENCODE_VERSION=1.15.7
ARG AISDK_OPENAI_COMPAT_VERSION=2.0.47

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

# Session storage on a durable Fly volume (plan §4.2 — sessions must outlive a
# restart). OpenCode keeps state under $XDG_DATA_HOME/opencode.
# VERIFY against the OpenCode docs that XDG_DATA_HOME covers the session DB.
ENV XDG_DATA_HOME=/data
VOLUME ["/data"]

WORKDIR /workspace
EXPOSE 4096

# Bind to 0.0.0.0 so the orchestrator can reach it. The server is protected by
# HTTP Basic — set OPENCODE_SERVER_PASSWORD as a Fly secret (never bake it in).
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
