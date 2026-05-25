# Pin to a python:3.12 slim image — sentence-transformers + torch CPU
# brings the heavy bits, no need for the full Python toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Cache HF models inside the image at build time so first request
    # doesn't pay the model-download tax.
    HF_HOME=/app/.hf-cache \
    TRANSFORMERS_OFFLINE=0 \
    # bge-small downloads ~130MB.
    SENTENCE_TRANSFORMERS_HOME=/app/.hf-cache

WORKDIR /app

# System deps: libgomp for torch's OpenMP.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# Split into two pip installs: torch-from-CPU-index first, then the rest
# from PyPI. Combining them in one command sometimes confuses pip about
# which index resolves which dep.
RUN pip install --no-cache-dir \
        torch==2.4.1+cpu \
        torchvision==0.19.1+cpu \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
        fastapi uvicorn[standard] pydantic "sentence-transformers>=3.0,<4"

# Pre-download the model so the first /embed request is fast.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('BAAI/bge-small-en-v1.5')"

COPY embeddings_shim /app/embeddings_shim

EXPOSE 8080

CMD ["uvicorn", "embeddings_shim.main:app", "--host", "0.0.0.0", "--port", "8080"]
