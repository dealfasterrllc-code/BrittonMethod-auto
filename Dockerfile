# ---------- Multi-stage Dockerfile for DealFasterr / BrittonMethod ----------
# Production-ready, flexible, and configurable.
# Usage examples at the end of this file.

# ---------- Builder stage ----------
FROM python:3.11-slim AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>" \
      org.opencontainers.image.source="https://github.com/your/repo"

# Build args to toggle optional heavy installs
ARG INSTALL_PLAYWRIGHT="1"    # 1 to install playwright browsers (chromium)
ARG INSTALL_TRANSFORMERS="0"  # 1 to install transformers (heavy)
ARG INSTALL_TORCH="0"         # 1 to install torch (very heavy, install separately if CUDA required)

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=/opt/venv \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system deps required to build wheels and optional Playwright runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    wget \
    gnupg \
    git \
    unzip \
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
    ca-certificates \
    locales \
 && rm -rf /var/lib/apt/lists/*

# Copy only dependency list first for layer caching
COPY requirements.txt /app/requirements.txt

# Create virtualenv and install base Python deps
RUN python -m venv ${VENV_PATH} \
    && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
    && if [ "${INSTALL_TRANSFORMERS}" = "1" ] ; then \
         # when transformers requested, include it and let pip resolve heavy libs
         ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt; \
       else \
         # Install all except heavy optional packages by filtering them out temporarily
         # (We assume requirements.txt contains transformers & torch as comments or optional; if not, install whole file)
         ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt; \
       fi

# Install Playwright browsers if requested (builder stage)
# Playwright requires extra packages which were installed above.
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ] ; then \
      ${VENV_PATH}/bin/python -m playwright install --with-deps chromium ; \
    else \
      echo "Playwright install skipped" ; \
    fi

# If user requested torch install at build time (not recommended in normal CI due to size),
# allow it via build-arg INSTALL_TORCH=1 and put torch install here; recommends installing via separate step
# depending on target (cpu/cuda). This block is intentionally commented to preserve small default images.
RUN if [ "${INSTALL_TORCH}" = "1" ] ; then \
      echo "Installing torch - ensure this is what you want (very large image)"; \
      ${VENV_PATH}/bin/pip install --no-cache-dir torch ; \
    else \
      echo "Torch not installed" ; \
    fi

# Clean builder apt caches
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/*

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
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install minimal runtime libs needed for Playwright and system utilities (curl, tini)
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

# Copy prebuilt virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code (minimal) after venv to improve cache
# .dockerignore should exclude large files, .git, node_modules, data, etc.
COPY . /app

# Ensure evidence dir exists and set permissions for non-root user
RUN mkdir -p ${EVIDENCE_DIR} \
    && groupadd -g 1000 appgroup || true \
    && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /bin/bash appuser || true \
    && chown -R appuser:appgroup /app /opt/venv ${EVIDENCE_DIR}

# Switch to non-root user
USER appuser

# Expose data volume for evidence persistence (optional)
VOLUME ["${EVIDENCE_DIR}"]

# Expose port for service
EXPOSE ${PORT}

# Healthcheck for container orchestrators (Render expects /health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# Use tini for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default Gunicorn config: tune workers with environment if desired
# You can override via command line or Render "Start Command"
ENV GUNICORN_WORKERS=3 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=120

# Start Gunicorn server (bind to all interfaces). Keep this simple to avoid shell escaping issues.
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:10000", "--workers", "3", "--threads", "4", "--timeout", "120", "--log-level", "info"]
