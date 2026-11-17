FROM node:24-bookworm-slim AS javascript

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALIR_MODE=worker \
    ALIR_STORAGE_DIR=/storage

# A real Node runtime and system FFmpeg/ffprobe are available for yt-dlp.
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 alir \
    && mkdir /storage \
    && chown alir:alir /storage \
    && node --version \
    && ffmpeg -version

WORKDIR /app
COPY requirements.txt requirements-worker.txt ./
RUN pip install --no-cache-dir -r requirements-worker.txt
COPY app ./app
USER alir
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# One process owns the SQLite queue. Inflight work is recovered from the mounted volume.
# Signed file URLs are intentionally omitted from access logs.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "25"]
