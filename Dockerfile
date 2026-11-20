# Runtime image for the DarGlobal & Wasalt assistant.
# Base pinned by digest (multi-arch OCI index: linux/amd64 for Hugging Face, linux/arm64 for local Macs).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=7860

# Hugging Face Spaces runs containers as UID 1000; code and data stay root-owned (read-only for the app).
RUN useradd --create-home --uid 1000 app
WORKDIR /srv

# Hash-locked, fully pinned dependency set; --no-deps prevents any unpinned resolution.
COPY requirements.txt .
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY app ./app
COPY data/corpus.jsonl data/manifest.json data/trap_report.json ./data/
# Host file modes are copied as-is; make code/data world-readable (not writable) for the UID 1000 user.
RUN chmod -R a+rX,go-w /srv/app /srv/data

USER app
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '7860'), timeout=4)"]

# Secrets (OPENROUTER_API_KEY) are injected at runtime by the host, never baked into the image.
CMD ["python", "-m", "app"]
