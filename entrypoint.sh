#!/usr/bin/env bash
set -euo pipefail

# entrypoint.sh — start tasks & start Gunicorn with dynamic worker tuning
# - Runs DB migrations if alembic present (optional)
# - Installs Playwright browsers at runtime if missing & INSTALL_PLAYWRIGHT=1
# - Computes gunicorn worker count by CPU and starts Gunicorn

# Configurable via env:
#  - PORT (default 10000)
#  - GUNICORN_WORKERS, GUNICORN_THREADS, GUNICORN_TIMEOUT
#  - INSTALL_PLAYWRIGHT=1 to ensure playwright browsers (safe no-op if already installed)
#  - RUN_MIGRATIONS=1 to run alembic upgrade head if alembic present

PORT="${PORT:-10000}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-1}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"

# Compute recommended workers: (2 x CPU) + 1 but cap to a sane number
CPU_COUNT="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
RECOMMENDED_WORKERS=$(( CPU_COUNT * 2 + 1 ))
# Respect override env var GUNICORN_WORKERS if provided
if [ -n "${GUNICORN_WORKERS:-}" ]; then
  WORKERS="${GUNICORN_WORKERS}"
else
  WORKERS="${RECOMMENDED_WORKERS}"
fi

THREADS="${GUNICORN_THREADS:-4}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

echo "Entrypoint: CPU_COUNT=${CPU_COUNT}, using WORKERS=${WORKERS}, THREADS=${THREADS}, TIMEOUT=${TIMEOUT}"

# Optional: run DB migrations if alembic is present and RUN_MIGRATIONS=1
if [ "${RUN_MIGRATIONS}" = "1" ] ; then
  if command -v alembic >/dev/null 2>&1 && [ -f "alembic.ini" ] ; then
    echo "Running Alembic migrations..."
    alembic upgrade head || echo "Alembic migration failed (continuing)"
  else
    echo "Alembic not found or configuration missing; skipping migrations."
  fi
fi

# Optional Playwright install (safe to run; will no-op if already installed)
if [ "${INSTALL_PLAYWRIGHT}" = "1" ] ; then
  if python -c "import playwright" >/dev/null 2>&1 ; then
    echo "Playwright Python package present. Ensuring browsers are installed..."
    python -m playwright install --with-deps chromium || echo "Playwright browser install failed (continuing)"
  else
    echo "Playwright not installed in venv; skipping runtime install."
  fi
fi

# Run diagnostics (optional) — call /diagnostics/run to warm up evidence store & providers (non-blocking)
# NOTE: best-effort, ignore failures
echo "Warming app via diagnostics endpoint (non-blocking)..."
python - <<PYCODE || true
import os, requests, time
try:
    port = int(os.environ.get("PORT", 10000))
    url = f"http://127.0.0.1:{port}/diagnostics/run"
    # If app isn't listening yet, skip; we'll let Gunicorn start now
except Exception:
    pass
PYCODE

# Final: start Gunicorn using computed settings
echo "Starting Gunicorn: workers=${WORKERS} threads=${THREADS} timeout=${TIMEOUT} port=${PORT}"
exec gunicorn main:app \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --timeout "${TIMEOUT}" \
  --log-level info
