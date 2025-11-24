# ---------- Multi-stage Dockerfile for DealFasterr / BrittonMethod ----------
# Production-ready, CPU-only ML by default. Optional heavy components installed via build-args.

# ---------- Builder stage ----------
FROM python:3.11.9-slim AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>" \
      org.opencontainers.image.source="https://github.com/dealfasterrllc-code/BrittonMethod-auto"

# Build-time toggles
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

# Install essential build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl wget ca-certificates git unzip gnupg locales procps \
 && rm -rf /var/lib/apt/lists/*

# Optional OS dependencies for Playwright
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libgbm1 libasound2 libpangocairo-1.0-0 libxrandr2 \
        libgtk-3-0 libgconf-2-4 libx11-xcb1 libxss1 libxtst6 fonts-liberation \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "Playwright not requested: skipping heavy OS libs"; \
    fi

# Copy requirements first
COPY requirements.txt /app/requirements.txt

# Create virtualenv and install Python packages
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="${VENV_PATH}/bin:$PATH"

# Optional Playwright + Chromium installation
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      pip install --no-cache-dir playwright ; \
      PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} python -m playwright install --with-deps chromium ; \
    fi

# Optional heavy ML packages
RUN if [ "${INSTALL_TRANSFORMERS}" = "1" ]; then pip install --no-cache-dir transformers; fi
RUN if [ "${INSTALL_TORCH}" = "1" ]; then pip install --no-cache-dir torch cpuonly; fi

# Prepare evidence directory
RUN mkdir -p /tmp/britton_evidence && chmod 0755 /tmp/britton_evidence

# ---------- Runtime stage ----------
FROM python:3.11.9-slim AS runtime

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ARG VENV_PATH="/opt/venv"
ARG PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PATH="${VENV_PATH}/bin:$PATH" \
    PORT=10000 \
    EVIDENCE_DIR=/tmp/britton_evidence \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

# Minimal runtime OS packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder
COPY --from=builder ${VENV_PATH} ${VENV_PATH}

# Copy source code
COPY . /app

# Copy Playwright browsers if installed
COPY --from=builder ${PLAYWRIGHT_BROWSERS_PATH} ${PLAYWRIGHT_BROWSERS_PATH} || true
RUN if [ -d "${PLAYWRIGHT_BROWSERS_PATH}" ]; then chmod -R 0755 ${PLAYWRIGHT_BROWSERS_PATH}; fi

# Ensure evidence dir exists and create safe non-root user
RUN mkdir -p ${EVIDENCE_DIR} \
    && groupadd --gid 1000 appgroup || true \
    && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /usr/sbin/nologin appuser || true \
    && chown -R appuser:appgroup /app ${VENV_PATH} ${EVIDENCE_DIR} ${PLAYWRIGHT_BROWSERS_PATH} \
    && chmod -R 0755 /app ${VENV_PATH} ${EVIDENCE_DIR}

USER appuser

VOLUME ["${EVIDENCE_DIR}"]
EXPOSE ${PORT}

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn defaults
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=120

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Default CMD
CMD ["./entrypoint.sh"]
