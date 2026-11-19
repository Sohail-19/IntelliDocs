FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY services/intelligence/pyproject.toml services/intelligence/uv.lock ./
COPY services/intelligence/src ./src
COPY services/intelligence/app ./app
ARG PARSING_EXTRAS=tesseract,formats
RUN pip install --no-cache-dir ".[${PARSING_EXTRAS}]"

FROM python:3.11-slim AS runtime

ARG TESSERACT_LANGUAGES=eng,hin
ENV TESSERACT_LANGUAGES=${TESSERACT_LANGUAGES} \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
RUN set -eu; packages=""; \
    for language in $(printf '%s' "$TESSERACT_LANGUAGES" | tr ',' ' '); do \
      case "$language" in *[!a-z0-9_-]*|'') echo "Invalid Tesseract language pack" >&2; exit 1;; esac; \
      packages="$packages tesseract-ocr-$language"; \
    done; \
    apt-get update && apt-get install -y --no-install-recommends tesseract-ocr $packages \
      && rm -rf /var/lib/apt/lists/*
RUN groupadd --system app && useradd --system --gid app --create-home app
RUN mkdir -p /data/uploads && chown app:app /data/uploads
COPY config /config
COPY contracts/document-upload-formats.v1.json /contracts/
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORG_CONFIG_DIR=/config/orgs \
    STORAGE_DIR=/data/uploads

USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn intellidocs_intelligence.main:app --host 0.0.0.0 --port ${INTELLIGENCE_PORT:-8000}"]
