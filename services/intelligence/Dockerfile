FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY services/intelligence/pyproject.toml services/intelligence/uv.lock ./
COPY services/intelligence/src ./src
RUN pip install --no-cache-dir .

FROM python:3.11-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn intellidocs_intelligence.main:app --host 0.0.0.0 --port ${INTELLIGENCE_PORT:-8000}"]
