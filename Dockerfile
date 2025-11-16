# ---------- Builder stage ----------
FROM python:3.11-slim AS builder

LABEL maintainer="DealFasterr / BrittonMethod"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=/opt/venv

# System deps for building wheels / Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    wget \
    gnupg \
    git \
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency file
COPY requirements.txt /app/requirements.txt

# Create venv and install Python deps
RUN python -m venv ${VENV_PATH} \
    && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
    && ${VENV_PATH}/bin/pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers (chromium) if needed
RUN ${VENV_PATH}/bin/python -m playwright install --with-deps chromium

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

LABEL maintainer="DealFasterr / BrittonMethod"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    VENV_PATH=/opt/venv \
    PORT=10000 \
    EVIDENCE_DIR=/tmp/britton_evidence

# Minimal runtime libs for Chromium / tini
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy app code
COPY . /app

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app /opt/venv /tmp/britton_evidence

USER appuser

# Persist evidence directory
VOLUME /tmp/britton_evidence

# Expose port
EXPOSE ${PORT}

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# Use tini for signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn
CMD gunicorn main:app --bind 0.0.0.0:$PORT --workers 3 --threads 4 --timeout 120 --log-level info
