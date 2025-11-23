# ---------- Multi-stage Dockerfile for DealFasterr / BrittonMethod ----------
# Production-ready. Installs Playwright browsers at build-time and copies to runtime.
# Keep heavy optional installs controlled by build-args.

# ---------- Builder stage ----------
FROM python:3.11-slim AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>" \
      org.opencontainers.image.source="https://github.com/dealfasterrllc-code/BrittonMethod-auto"

# Build-time toggles (set via docker build --build-arg ...)
ARG INSTALL_PLAYWRIGHT="1"
ARG INSTALL_TRANSFORMERS="0"
ARG INSTALL_TORCH="0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system deps required for building wheels and Playwright/browser libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    wget \
    gnupg \
    git \
    unzip \
    netcat \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxrandr2 \
    libgtk-3-0 \
    libgconf-2-4 \
    libx11-xcb1 \
    libxss1 \
    libxtst6 \
    locales \
 && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for Docker layer caching
COPY requirements.txt /app/requirements.txt

# Create virtualenv and install dependencies
RUN python -m venv ${VENV_PATH} \
    && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel

# Install Python deps into venv. If you want to skip heavy libs, manage requirements.txt accordingly.
RUN ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="${VENV_PATH}/bin:$PATH"

# Install Playwright Python package + browsers in builder (if requested)
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir playwright || true; \
      # install browsers to PLAYWRIGHT_BROWSERS_PATH so we can copy them to runtime
      PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} ${VENV_PATH}/bin/python -m playwright install --with-deps chromium || true; \
    else \
      echo "Playwright skipped at build-time"; \
    fi

# Optional: install transformers/torch if build-args set (left minimal by default)
RUN if [ "${INSTALL_TRANSFORMERS}" = "1" ] ; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir transformers ; \
    else \
      echo "Transformers not installed (use build-arg INSTALL_TRANSFORMERS=1)"; \
    fi

RUN if [ "${INSTALL_TORCH}" = "1" ] ; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir torch ; \
    else \
      echo "Torch not installed (use build-arg INSTALL_TORCH=1)"; \
    fi

# Make evidence dir in builder (will be created/copied to runtime)
RUN mkdir -p /tmp/britton_evidence && chmod -R a+rwx /tmp/britton_evidence

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ARG INSTALL_PLAYWRIGHT="1"
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=/opt/venv \
    PORT=10000 \
    EVIDENCE_DIR=/tmp/britton_evidence \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Minimal runtime libs for Playwright and utilities (tini for signal handling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxrandr2 \
    libgtk-3-0 \
    libgconf-2-4 \
    libx11-xcb1 \
    libxss1 \
    libxtst6 \
    tini \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder (includes Python packages)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
COPY . /app

# Copy Playwright browsers directory if it exists in builder
# (builder installed browsers to PLAYWRIGHT_BROWSERS_PATH=/ms-playwright)
COPY --from=builder /ms-playwright /ms-playwright || true
RUN chmod -R a+rwx /ms-playwright || true

# Ensure evidence dir exists and create non-root user
RUN mkdir -p ${EVIDENCE_DIR} \
    && groupadd -g 1000 appgroup || true \
    && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /bin/bash appuser || true \
    && chown -R appuser:appgroup /app /opt/venv ${EVIDENCE_DIR} /ms-playwright || true

USER appuser

VOLUME ["${EVIDENCE_DIR}"]
EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default gunicorn-related ENV (can be overridden)
ENV GUNICORN_WORKERS=3 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=120

# Default: call entrypoint script (ensure entrypoint.sh exists & is executable in repo root)
# If you prefer to bypass entrypoint use: CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:10000"]
CMD ["./entrypoint.sh"]
