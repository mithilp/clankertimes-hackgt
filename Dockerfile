# One image for every worker role; the compose file picks the role.
# Playwright's image ships Chromium and its system libraries, pinned to the pip version below.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/models \
    MEM0_TELEMETRY=False \
    MEM0_HISTORY_DB=/data/mem0/history.db \
    ARCHIVE_DIR=/data/archive \
    BEATS_FILE=/app/config/beats.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so workers never download it at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models')"

COPY newsroom ./newsroom
COPY db ./db
COPY config ./config

ENTRYPOINT ["python", "-m", "newsroom"]
