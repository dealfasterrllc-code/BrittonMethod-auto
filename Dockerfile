# ---------- Multi-stage Dockerfile for DealFasterr / BrittonMethod ----------
# Production-ready. Builds optional heavy components only when requested via build-args.
# Defaults are safe for small cloud hosts (Playwright and heavy LLM libs are OFF).

# ---------- Builder stage ----------
FROM python:3.11-slim AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>" \
      org.opencontainers.image.source="https://github.com/dealfasterrllc-code/BrittonMethod-auto"

# Build-time toggles (use docker build --build-arg ...)
ARG INSTALL_PLAYWRIGHT="0"
ARG INSTALL_TRANSFORMERS="0"
ARG INSTALL_TORCH="0"
ARG VENV_PATH="/opt/venv"
ARG PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}

WORKDIR /app

# Install minimal build deps common to all builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl wget ca-certificates git unzip gnupg \
    locales procps \
 && rm -rf /var/lib/apt/lists/*

# If Playwright is requested, install extra OS libs required for browsers (Chromium)
# Note: this block runs only if INSTALL_PLAYWRIGHT=1 at build time.
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libgbm1 libasound2 libpangocairo-1.0-0 libxrandr2 \
        libgtk-3-0 libgconf-2-4 libx11-xcb1 libxss1 libxtst6 fonts-liberation \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "Playwright not requested: skipping heavy OS libs"; \
    fi

# Copy only requirements first for Docker layer caching
COPY requirements.txt /app/requirements.txt

# Create virtualenv and install Python runtime deps
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="${VENV_PATH}/bin:$PATH"

# Optional Playwright python package + browsers in builder (if requested)
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      echo "Installing Playwright package and browsers (builder)..." ; \
      ${VENV_PATH}/bin/pip install --no-cache-dir playwright || true ; \
      # Install browsers to PLAYWRIGHT_BROWSERS_PATH
      PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} ${VENV_PATH}/bin/python -m playwright install --with-deps chromium || true ; \
    else \
      echo "Skipping Playwright installation in builder"; \
    fi

# Optional heavy Python libs (transformers / torch) only when requested
RUN if [ "${INSTALL_TRANSFORMERS}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir transformers || true ; \
    else \
      echo "Skipping transformers"; \
    fi

RUN if [ "${INSTALL_TORCH}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir torch || true ; \
    else \
      echo "Skipping torch"; \
    fi

# Create evidence dir in builder so we can copy it (with safe perms)
RUN mkdir -p /tmp/britton_evidence && chmod 0755 /tmp/britton_evidence

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ARG VENV_PATH="/opt/venv"
ARG PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PATH="${VENV_PATH}/bin:$PATH" \
    PORT=8000 \
    EVIDENCE_DIR=/tmp/britton_evidence \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

# Minimal runtime OS packages; keep small unless Playwright browsers are used
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy virtualenv (Python packages) from builder
COPY --from=builder ${VENV_PATH} ${VENV_PATH}

# Copy app source
COPY . /app

# Copy Playwright browsers directory from builder if present (best-effort)
COPY --from=builder ${PLAYWRIGHT_BROWSERS_PATH} ${PLAYWRIGHT_BROWSERS_PATH} || true
# Ensure safe permissions (do not make world-writable)
RUN if [ -d "${PLAYWRIGHT_BROWSERS_PATH}" ]; then chmod -R 0755 ${PLAYWRIGHT_BROWSERS_PATH} || true; fi

# Ensure evidence dir exists and create non-root user with safe ownership
RUN mkdir -p ${EVIDENCE_DIR} \
    && groupadd --gid 1000 appgroup || true \
    && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /usr/sbin/nologin appuser || true \
    && chown -R appuser:appgroup /app ${VENV_PATH} ${EVIDENCE_DIR} ${PLAYWRIGHT_BROWSERS_PATH} || true \
    && chmod -R 0755 /app ${VENV_PATH} ${EVIDENCE_DIR} || true

USER appuser

VOLUME ["${EVIDENCE_DIR}"]
EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default gunicorn-related ENV (conservative defaults for small hosts)
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=120 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# Ensure entrypoint.sh is executable (entrypoint is expected in repo root)
RUN if [ -f /app/entrypoint.sh ]; then chmod +x /app/entrypoint.sh || true; fi

# Default to using the repository entrypoint script
CMD ["./entrypoint.sh"]
