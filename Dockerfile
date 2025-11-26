# syntax=docker/dockerfile:1
# Multi-stage, production-ready Dockerfile for DealFasterr / BrittonMethod
# - CPU-first design, optional heavy components via build args
# - Optimized for small runtime image, reproducible builds, and non-root runtime

########################################
# Build stage: install build deps, create venv, install pip deps
########################################
ARG PYTHON_VERSION=3.12.2-slim
ARG VENV_PATH=/opt/venv
ARG INSTALL_PLAYWRIGHT=0
ARG INSTALL_TRANSFORMERS=0
ARG INSTALL_TORCH=0
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

FROM python:${PYTHON_VERSION} AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}

WORKDIR /app

# Keep minimal build dependencies, remove lists after use to keep layer small
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential curl wget ca-certificates git unzip gnupg locales procps ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Optional OS packages for Playwright (installed only if requested)
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libgbm1 libasound2 libpangocairo-1.0-0 libxrandr2 libgtk-3-0 libgconf-2-4 libx11-xcb1 libxss1 libxtst6 fonts-liberation \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "Playwright not requested: skipping heavy OS libs"; \
    fi

# Copy only requirements early to leverage cache
COPY requirements.txt /app/requirements.txt

# Create python venv and install requirements (no cache) — isolate build-time artifacts in builder
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="${VENV_PATH}/bin:$PATH"

# Optional: install Playwright and browsers (heavy)
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir playwright && \
      PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} ${VENV_PATH}/bin/python -m playwright install --with-deps chromium; \
    else \
      echo "Skipping playwright install"; \
    fi

# Optional: transformers / accelerate
RUN if [ "${INSTALL_TRANSFORMERS}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir transformers accelerate ; \
    else \
      echo "Skipping transformers install"; \
    fi

# Optional: torch (CPU-only recommended) — allow pip fallback
RUN if [ "${INSTALL_TORCH}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir "torch==2.2.0" || ${VENV_PATH}/bin/pip install --no-cache-dir torch ; \
    else \
      echo "Skipping torch install"; \
    fi

# Create evidence dir in builder so it can be copied to runtime
RUN mkdir -p /tmp/britton_evidence && chmod 0755 /tmp/britton_evidence

# Copy application source (only after deps are installed to maximize cache reuse)
# We copy everything in builder so that any build-time tools can access code (e.g. yarn/npm, model caches)
COPY . /app

########################################
# Runtime stage: minimal OS, copy venv and app, run as non-root
########################################
FROM python:${PYTHON_VERSION} AS runtime

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ARG VENV_PATH=/opt/venv
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG PORT=10000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PATH="${VENV_PATH}/bin:$PATH" \
    PORT=${PORT} \
    EVIDENCE_DIR=/tmp/britton_evidence \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    MODEL_PROVIDER_PRIMARY=GEMINI

WORKDIR /app

# Minimal runtime OS packages — tini is used to reap processes correctly
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl ca-certificates gnupg netcat \
 && rm -rf /var/lib/apt/lists/*

# Copy venv from builder to runtime
COPY --from=builder ${VENV_PATH} ${VENV_PATH}

# Copy application from builder
COPY --from=builder /app /app

# If playwright browsers were installed in builder, copy them (no-op if absent)
COPY --from=builder ${PLAYWRIGHT_BROWSERS_PATH} ${PLAYWRIGHT_BROWSERS_PATH} || true

# Ensure evidence dir exists, create non-root user and set proper ownership
RUN mkdir -p ${EVIDENCE_DIR} \
 && groupadd --gid 1000 appgroup || true \
 && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /usr/sbin/nologin appuser || true \
 && chown -R appuser:appgroup /app ${VENV_PATH} ${EVIDENCE_DIR} ${PLAYWRIGHT_BROWSERS_PATH} || true \
 && chmod -R 0755 /app ${VENV_PATH} ${EVIDENCE_DIR} || true

USER appuser

VOLUME ["${EVIDENCE_DIR}"]
EXPOSE ${PORT}

# Healthcheck (calls the app's /health endpoint). Uses netcat/curl minimal checks
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn defaults (overridable at runtime)
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=120

# Ensure entrypoint script is executable (expected at /app/entrypoint.sh)
RUN if [ -f /app/entrypoint.sh ]; then chmod +x /app/entrypoint.sh; fi

# Default command: entrypoint should exec gunicorn or fallback to python main
CMD ["./entrypoint.sh"]

########################################
# Build-time usage examples (local)
########################################
# docker build -t brittonmethod:latest .
# docker build --build-arg INSTALL_PLAYWRIGHT=1 --build-arg INSTALL_TRANSFORMERS=0 --build-arg INSTALL_TORCH=0 -t brittonmethod:with-ml:latest .

# Runtime examples
# docker run --rm -it -p 10000:10000 -v $(pwd)/.env:/app/.env:ro -v $(pwd)/evidence:/tmp/britton_evidence --env PORT=10000 brittonmethod:latest

# Production example (systemd/k8s/docker-compose): mount .env via secrets and evidence storage as a persistent volume

