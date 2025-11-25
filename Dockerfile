# From repo root
cd ~/BrittonMethod-auto || { echo "cd failed"; exit 1; }

# Backup existing Dockerfile if present
if [ -f Dockerfile ]; then cp Dockerfile Dockerfile.bak.$(date +%s); echo "Backed up Dockerfile"; fi

# Write the new upgraded Dockerfile
cat > Dockerfile <<'DOCKER'
# ---------- Multi-stage Dockerfile for DealFasterr / BrittonMethod ----------
# Production-ready, CPU-first ML by default. Optional heavy components installed via build-args.
# Designed for small runtime image, reproducible builds, and safe non-root runtime.

# Build-time knobs:
#  - INSTALL_PLAYWRIGHT=1  => installs Playwright & browsers (heavy)
#  - INSTALL_TRANSFORMERS=1 => installs 'transformers' package (heavy)
#  - INSTALL_TORCH=1       => installs torch (heavy, CPU-only variant)
#  - PYTHON_VERSION        => defaults to 3.12-slim
ARG PYTHON_VERSION=3.12.2-slim
ARG INSTALL_PLAYWRIGHT=0
ARG INSTALL_TRANSFORMERS=0
ARG INSTALL_TORCH=0
ARG VENV_PATH=/opt/venv
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

FROM python:${PYTHON_VERSION} AS builder

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>" \
      org.opencontainers.image.source="https://github.com/dealfasterrllc-code/BrittonMethod-auto"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}

WORKDIR /app

# Install essential build dependencies
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential curl wget ca-certificates git unzip gnupg locales procps ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Optional: install Playwright OS runtime deps only when requested
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libgbm1 libasound2 libpangocairo-1.0-0 libxrandr2 \
        libgtk-3-0 libgconf-2-4 libx11-xcb1 libxss1 libxtst6 fonts-liberation \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "Playwright not requested: skipping heavy OS libs"; \
    fi

# Copy requirements early to leverage layer caching
COPY requirements.txt /app/requirements.txt

# Create isolated venv and install dependencies
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="${VENV_PATH}/bin:$PATH"

# Optional Playwright install (browsers). Only executes when INSTALL_PLAYWRIGHT=1
RUN if [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir playwright && \
      PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} ${VENV_PATH}/bin/python -m playwright install --with-deps chromium; \
    else \
      echo "Skipping playwright install"; \
    fi

# Optional transformer packages (keep separate to minimize default image size)
RUN if [ "${INSTALL_TRANSFORMERS}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir transformers accelerate ; \
    else \
      echo "Skipping transformers install"; \
    fi

# Optional torch install (CPU-only recommended). Toggle with INSTALL_TORCH=1
RUN if [ "${INSTALL_TORCH}" = "1" ]; then \
      ${VENV_PATH}/bin/pip install --no-cache-dir "torch==2.2.0" || ${VENV_PATH}/bin/pip install --no-cache-dir torch ; \
    else \
      echo "Skipping torch install"; \
    fi

# Ensure evidence dir exists in build image (copied to runtime later)
RUN mkdir -p /tmp/britton_evidence && chmod 0755 /tmp/britton_evidence

# ---------- Runtime stage ----------
FROM python:${PYTHON_VERSION} AS runtime

LABEL maintainer="DealFasterr / BrittonMethod <ops@dealfasterr.com>"

ARG VENV_PATH=/opt/venv
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=${VENV_PATH} \
    PATH="${VENV_PATH}/bin:$PATH" \
    PORT=10000 \
    EVIDENCE_DIR=/tmp/britton_evidence \
    PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    MODEL_PROVIDER_PRIMARY=GEMINI

WORKDIR /app

# Minimal runtime OS packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl ca-certificates gnupg \
 && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder
COPY --from=builder ${VENV_PATH} ${VENV_PATH}

# Copy application code
COPY . /app

# If Playwright browsers were installed in builder, copy them (if present)
COPY --from=builder ${PLAYWRIGHT_BROWSERS_PATH} ${PLAYWRIGHT_BROWSERS_PATH} || true
RUN if [ -d "${PLAYWRIGHT_BROWSERS_PATH}" ]; then chmod -R 0755 ${PLAYWRIGHT_BROWSERS_PATH}; fi

# Ensure evidence dir exists, create non-root user, set ownership
RUN mkdir -p ${EVIDENCE_DIR} \
 && groupadd --gid 1000 appgroup || true \
 && useradd --create-home --no-log-init --uid 1000 --gid appgroup --shell /usr/sbin/nologin appuser || true \
 && chown -R appuser:appgroup /app ${VENV_PATH} ${EVIDENCE_DIR} ${PLAYWRIGHT_BROWSERS_PATH} || true \
 && chmod -R 0755 /app ${VENV_PATH} ${EVIDENCE_DIR} || true

USER appuser

VOLUME ["${EVIDENCE_DIR}"]
EXPOSE ${PORT}

# Healthcheck (calls the app's /health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn defaults (overridable at runtime)
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=120

# Ensure entrypoint script is executable (if present)
RUN if [ -f /app/entrypoint.sh ]; then chmod +x /app/entrypoint.sh; fi

# Default command:
# entrypoint.sh should handle environment prep and then exec gunicorn or fallback to python main
# If you prefer to run python directly in lightweight dev, override CMD when running container.
CMD ["./entrypoint.sh"]
DOCKER

# Print confirmation & suggested build commands
echo "Wrote upgraded Dockerfile."

cat <<'GUIDE'

=== Build notes & suggested commands ===

# Build default (small):
docker build -t brittonmethod:latest .

# Build with Gemini/Playwright/Transformers/Torch (heavy). Example:
# - INSTALL_PLAYWRIGHT=1 pulls Playwright and chromium (large)
# - INSTALL_TRANSFORMERS=1 installs 'transformers' and 'accelerate'
# - INSTALL_TORCH=1 installs torch (CPU)
docker build --build-arg INSTALL_PLAYWRIGHT=0 \
             --build-arg INSTALL_TRANSFORMERS=0 \
             --build-arg INSTALL_TORCH=0 \
             -t brittonmethod:latest .

# Example with heavy flags:
# docker build --build-arg INSTALL_PLAYWRIGHT=1 --build-arg INSTALL_TRANSFORMERS=1 --build-arg INSTALL_TORCH=0 -t brittonmethod:with-ml:latest .

# Run container (development):
# Mount your .env and evidence dir if desired:
docker run --rm -it -p 10000:10000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/evidence:${EVIDENCE_DIR} \
  --env PORT=10000 \
  brittonmethod:latest

# Production (example with env file):
docker run -d --restart unless-stopped --name brittonmethod \
  --env-file .env -p 10000:10000 -v $(pwd)/evidence:${EVIDENCE_DIR} brittonmethod:latest

GUIDE
