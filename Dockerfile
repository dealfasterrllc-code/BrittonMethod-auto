# ---------- Builder ----------
FROM python:3.11-slim AS builder

# system deps needed for Playwright + Chromium and common build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    wget \
    gnupg \
    ca-certificates \
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

# copy only what's needed to install deps (cache layer)
COPY requirements.txt .

ENV PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Install Playwright browsers (chromium). This must be done after pip install playwright
RUN python -m playwright install chromium

# ---------- Runtime ----------
FROM python:3.11-slim

# Minimal runtime deps for chromium
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed site-packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . /app

# Create a non-root user for safety
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# Gunicorn recommended; hit the same module entry as you have (Main:app)
EXPOSE ${PORT}

# Use a default command; Render/other platforms can override
CMD ["gunicorn", "Main:app", "--bind", "0.0.0.0:10000", "--workers", "3", "--timeout", "120"]
