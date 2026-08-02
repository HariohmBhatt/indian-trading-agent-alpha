FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

ARG OCI_SOURCE="https://github.com/HariohmBhatt/indian-trading-agent-alpha"
ARG OCI_REVISION="local"

LABEL org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${OCI_REVISION}"

ARG RELEASE_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

LABEL org.opencontainers.image.revision="${RELEASE_SHA}"

WORKDIR /app

COPY pyproject.toml requirements.lock README.md LICENSE NOTICE ./
COPY tradingagents ./tradingagents
COPY cli ./cli

RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation .

COPY backend ./backend
COPY scripts ./scripts
COPY main.py ./

RUN mkdir -p /data

FROM base AS production

ENV TRADINGAGENTS_HOME=/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS development

ENV TRADINGAGENTS_HOME=/data \
    WATCHFILES_FORCE_POLLING=true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/backend", "--reload-dir", "/app/tradingagents"]
