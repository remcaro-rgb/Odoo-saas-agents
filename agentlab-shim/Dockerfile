# Playwright base image — bundles Chromium + system deps.
# Pin to a specific tag so a future Playwright bump doesn't surprise us.
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

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
