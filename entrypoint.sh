#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# BrittonMethod-auto — entrypoint.sh
# Production-ready startup script for Render (or any containerized Python service)
#
# Features:
#  - Activates virtualenv if available
#  - Computes Gunicorn workers based on CPU, capped for safety
#  - Dynamically appends threads, timeout, and port flags
#  - Optional Alembic migrations
#  - Optional Playwright browser installation
#  - Waits for TCP dependencies (Redis, Postgres) if configured
#  - Ensures evidence directory exists and has correct permissions
#  - Non-blocking warmup HTTP call for diagnostics
#
# Configurable via environment variables:
#  - PORT (default 10000, Render injects $PORT automatically)
#  - GUNICORN_WORKERS, GUNICORN_THREADS, GUNICORN_TIMEOUT
#  - INSTALL_PLAYWRIGHT=1 (default 1)
#  - RUN_MIGRATIONS=1 (default 0)
#  - VENV_PATH (default /opt/venv)
#  - GUNICORN_CMD (override command, optional)
###############################################################################

# --- CONFIG ---
PORT="${PORT:-10000}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-1}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
GUNICORN_CMD="${GUNICORN_CMD:-}"

# --- ACTIVATE VIRTUALENV ---
if [ -d "${VENV_PATH}" ] && [ -f "${VENV_PATH}/bin/activate" ]; then
    source "${VENV_PATH}/bin/activate"
    echo "[INFO] Activated virtualenv at ${VENV_PATH}"
else
    echo "[WARN] No virtualenv found at ${VENV_PATH} — continuing without activation"
fi

# --- COMPUTE WORKERS / THREADS / TIMEOUT ---
CPU_COUNT="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
RECOMMENDED_WORKERS=$(( CPU_COUNT*2 + 1 ))
WORKERS="${GUNICORN_WORKERS:-$RECOMMENDED_WORKERS}"
THREADS="${GUNICORN_THREADS:-4}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

MAX_WORKERS_CAP="${MAX_WORKERS_CAP:-32}"
if [ "$WORKERS" -gt "$MAX_WORKERS_CAP" ]; then
    echo "[WARN] Worker count ${WORKERS} exceeds cap ${MAX_WORKERS_CAP}, reducing"
    WORKERS="$MAX_WORKERS_CAP"
fi

echo "[INFO] Gunicorn configuration — CPU: ${CPU_COUNT}, WORKERS: ${WORKERS}, THREADS: ${THREADS}, TIMEOUT: ${TIMEOUT}, PORT: ${PORT}"

# --- HELPER: WAIT FOR TCP DEPENDENCIES ---
wait_for_tcp() {
    local hostport=$1
    local timeout=${2:-30}
    [ -z "$hostport" ] && return 0
    IFS=':' read -r host port <<<"$hostport"
    echo "[INFO] Waiting for $host:$port (timeout ${timeout}s)..."
    for i in $(seq 1 "$timeout"); do
        nc -z "$host" "$port" >/dev/null 2>&1 && echo "[INFO] $host:$port is up" && return 0
        sleep 1
    done
    echo "[WARN] $host:$port did not become available after ${timeout}s"
    return 1
}

if command -v nc >/dev/null 2>&1; then
    [ -n "${REDIS_URL:-}" ] && REDIS_HOSTPORT=$(echo "${REDIS_URL}" | sed -E 's#^redis://([^/@]+@)?([^:]+:[0-9]+).*#\2#; t; s#.*##') && [ -n "$REDIS_HOSTPORT" ] && wait_for_tcp "$REDIS_HOSTPORT" 15
    [ -n "${DATABASE_URL:-}" ] && DB_HOSTPORT=$(echo "${DATABASE_URL}" | sed -E 's#^.*@([^:/]+:[0-9]+).*#\1#; t; s#.*##') && [ -n "$DB_HOSTPORT" ] && wait_for_tcp "$DB_HOSTPORT" 20
else
    echo "[WARN] nc (netcat) not available; skipping TCP wait checks"
fi

# --- OPTIONAL ALEMBIC MIGRATIONS ---
if [ "$RUN_MIGRATIONS" = "1" ] && command -v alembic >/dev/null 2>&1 && [ -f "alembic.ini" ]; then
    echo "[INFO] Running Alembic migrations..."
    alembic upgrade head || echo "[WARN] Alembic migration failed, continuing"
else
    echo "[INFO] Skipping Alembic migrations"
fi

# --- OPTIONAL PLAYWRIGHT INSTALL ---
if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then
    if python -c "import playwright" >/dev/null 2>&1; then
        echo "[INFO] Playwright detected — installing browsers..."
        python -m playwright install --with-deps chromium || echo "[WARN] Playwright install failed"
    else
        echo "[INFO] Playwright not installed, skipping browser install"
    fi
fi

# --- ENSURE EVIDENCE DIRECTORY ---
EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/britton_evidence}"
mkdir -p "$EVIDENCE_DIR"
chmod -R a+rwx "$EVIDENCE_DIR" || true
echo "[INFO] Evidence directory ready: $EVIDENCE_DIR"

# --- WARMUP DIAGNOSTICS CALL (NON-BLOCKING) ---
if command -v curl >/dev/null 2>&1; then
    (sleep 0.5 && curl -fsS "http://127.0.0.1:${PORT}/diagnostics/run" >/dev/null 2>&1) & disown || true
fi

# --- FINAL GUNICORN COMMAND ---
if [ -z "$GUNICORN_CMD" ]; then
    if [ -f "gunicorn_conf.py" ]; then
        GUNICORN_CMD="gunicorn -c gunicorn_conf.py main:app"
    else
        GUNICORN_CMD="gunicorn main:app"
    fi
fi

# Append dynamic flags if missing
[[ "$GUNICORN_CMD" != *"--workers"* ]] && GUNICORN_CMD="$GUNICORN_CMD --workers $WORKERS"
[[ "$GUNICORN_CMD" != *"--threads"* ]] && GUNICORN_CMD="$GUNICORN_CMD --threads $THREADS"
[[ "$GUNICORN_CMD" != *"--timeout"* ]] && GUNICORN_CMD="$GUNICORN_CMD --timeout $TIMEOUT"
[[ "$GUNICORN_CMD" != *"--bind"* ]] && GUNICORN_CMD="$GUNICORN_CMD --bind 0.0.0.0:$PORT"

echo "[INFO] Starting Gunicorn server: $GUNICORN_CMD"
exec $GUNICORN_CMD
