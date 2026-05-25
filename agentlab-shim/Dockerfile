# Playwright base image — bundles Chromium + system deps.
# Keep this tag pinned to match the `playwright` pip version in
# requirements.txt — a mismatch surfaces as an `Executable doesn't exist`
# error in the runner (canary #59 on 2026-05-24 caught a v1.42 image
# against a v1.60 pip install).
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agentlab_shim /app/agentlab_shim

# Fly's default port is 8080. The shim listens on it directly; no nginx.
EXPOSE 8080

# `uvicorn` directly (not gunicorn) — single worker per Fly machine is fine
# given Playwright's heavy per-request memory footprint; scale horizontally
# via Fly machine count, not threads.
CMD ["uvicorn", "agentlab_shim.main:app", "--host", "0.0.0.0", "--port", "8080"]
