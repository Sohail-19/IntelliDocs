FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY services/intelligence/pyproject.toml services/intelligence/uv.lock ./
COPY services/intelligence/src ./src
COPY services/intelligence/app ./app
ARG PARSING_EXTRAS=tesseract,formats
ARG PROVISION_E5_SMALL=false
RUN set -eu; mkdir -p /opt/models; extras="$PARSING_EXTRAS"; \
    case "$PROVISION_E5_SMALL" in \
      true) pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu 'torch>=2.5,<3'; \
            extras="$extras,sentence-transformers" ;; \
      false) ;; \
      *) echo 'Invalid PROVISION_E5_SMALL' >&2; exit 1 ;; \
    esac; \
    pip install --no-cache-dir ".[${extras}]"; \
    if [ "$PROVISION_E5_SMALL" = true ]; then \
      HF_HOME=/tmp/hf python -c 'from huggingface_hub import snapshot_download; snapshot_download("intfloat/multilingual-e5-small", revision="614241f622f53c4eeff9890bdc4f31cfecc418b3", local_dir="/opt/models/multilingual-e5-small", allow_patterns=["*.json", "sentencepiece.bpe.model", "model.safetensors"], ignore_patterns=["onnx/*", "openvino/*"])'; \
      rm -rf /tmp/hf /opt/models/multilingual-e5-small/.cache; \
    fi

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
ARG APP_UID=""
RUN set -eu; \
    if [ -n "$APP_UID" ]; then groupadd --gid "$APP_UID" app; useradd --uid "$APP_UID" --gid app --create-home app; \
    else groupadd --system app; useradd --system --gid app --create-home app; fi; \
    mkdir -p /data/uploads && chown app:app /data/uploads
COPY config /config
COPY contracts/document-upload-formats.v1.json /contracts/
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORG_CONFIG_DIR=/config/orgs \
    STORAGE_DIR=/data/uploads \
    EMBEDDING_LIGHTWEIGHT_MODEL=/opt/models/multilingual-e5-small

USER app
EXPOSE 8000
CMD ["python", "-m", "intellidocs_intelligence.serve"]
