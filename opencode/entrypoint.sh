#!/bin/sh
# Drop the long-running OpenCode service from root to the unprivileged
# `opencode` user (task #57 — bash-permission hardening).
#
# Runs as root only long enough to fix ownership of the /data Fly volume
# (Fly volumes mount root-owned), then re-exec's the server as `opencode` so
# the agent's bash and edit tools never hold root.
set -e
chown -R opencode:opencode /data 2>/dev/null || true
exec setpriv --reuid=opencode --regid=opencode --init-groups "$@"
