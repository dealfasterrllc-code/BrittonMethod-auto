#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# BrittonMethod-auto — entrypoint.sh
# Production-ready startup script for Render (or any containerized Python service)
###############################################################################

# --- CONFIG (can be overridden by env / Render) ---
# Accept first arg as port if provided, else use $PORT env, else default to 8000
PORT="${1:-${PORT:-8000}}"
# Playwright: default OFF in cloud builds; set INSTALL_PLAYWRIGHT=1 locally if you need it
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-0}"
# Respect explicit skip flag (used by Render)
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-0}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
GUNICORN_CMD="${GUNICORN_CMD:-}"
# Workers/threads defaults (can be overridden)
CPU_COUNT="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
RECOMMENDED_WORKERS=$(( CPU_COUNT*2 + 1 ))
GUNICORN_WORKERS="${GUNICORN_WORKERS:-$RECOMMENDED_WORKERS}"
GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
MAX_WORKERS_CAP="${MAX_WORKERS_CAP:-8}"   # keep a conservative cap for small hosts

# --- ACTIVATE VIRTUALENV (if present) ---
if [ -d "${VENV_PATH}" ] && [ -f "${VENV_PATH}/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "${VENV_PATH}/bin/activate"
    echo "[INFO] Activated virtualenv at ${VENV_PATH}"
else
    echo "[WARN] No virtualenv found at ${VENV_PATH} — continuing without activation"
fi

# --- ENFORCE caps / sane defaults ---
if [ "${GUNICORN_WORKERS}" -gt "${MAX_WORKERS_CAP}" ]; then
    echo "[WARN] Worker count ${GUNICORN_WORKERS} exceeds cap ${MAX_WORKERS_CAP}, reducing"
    GUNICORN_WORKERS="${MAX_WORKERS_CAP}"
fi

echo "[INFO] Gunicorn configuration — CPU: ${CPU_COUNT}, WORKERS: ${GUNICORN_WORKERS}, THREADS: ${GUNICORN_THREADS}, TIMEOUT: ${GUNICORN_TIMEOUT}, PORT: ${PORT}"

# --- HELPER: WAIT FOR TCP DEPENDENCIES (if nc available) ---
wait_for_tcp() {
    local hostport=$1
    local timeout=${2:-30}
    [ -z "$hostport" ] && return 0
    IFS=':' read -r host port <<<"$hostport"
    if [ -z "$host" ] || [ -z "$port" ]; then
        return 1
    fi
    echo "[INFO] Waiting for $host:$port (timeout ${timeout}s)..."
    for i in $(seq 1 "$timeout"); do
        nc -z "$host" "$port" >/dev/null 2>&1 && echo "[INFO] $host:$port is up" && return 0
        sleep 1
    done
    echo "[WARN] $host:$port did not become available after ${timeout}s"
    return 1
}

if command -v nc >/dev/null 2>&1; then
    if [ -n "${REDIS_URL:-}" ]; then
        REDIS_HOSTPORT=$(echo "${REDIS_URL}" | sed -E 's#^redis://([^/@]+@)?([^:]+:[0-9]+).*#\2#; t; s#.*##')
        [ -n "$REDIS_HOSTPORT" ] && wait_for_tcp "$REDIS_HOSTPORT" 15 || true
    fi
    if [ -n "${DATABASE_URL:-}" ]; then
        DB_HOSTPORT=$(echo "${DATABASE_URL}" | sed -E 's#^.*@([^:/]+:[0-9]+).*#\1#; t; s#.*##')
        [ -n "$DB_HOSTPORT" ] && wait_for_tcp "$DB_HOSTPORT" 20 || true
    fi
else
    echo "[WARN] nc (netcat) not available; skipping TCP wait checks"
fi

# --- OPTIONAL ALEMBIC MIGRATIONS ---
if [ "${RUN_MIGRATIONS}" = "1" ] && command -v alembic >/dev/null 2>&1 && [ -f "alembic.ini" ]; then
    echo "[INFO] Running Alembic migrations..."
    if ! alembic upgrade head; then
        echo "[WARN] Alembic migration failed; continuing"
    fi
else
    echo "[INFO] Skipping Alembic migrations"
fi

# --- OPTIONAL PLAYWRIGHT INSTALL (respect skip env) ---
if [ "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD}" = "1" ]; then
    echo "[INFO] PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 set; skipping Playwright browser install"
elif [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then
    if python -c "import playwright" >/dev/null 2>&1; then
        echo "[INFO] Playwright detected — attempting browser install (may require root)..."
        # best-effort; do not fail start if install fails
        if ! python -m playwright install chromium; then
            echo "[WARN] Playwright browser install failed; continuing without browsers"
        fi
    else
        echo "[INFO] Playwright not installed; skipping browser install"
    fi
else
    echo "[INFO] Playwright install disabled (INSTALL_PLAYWRIGHT != 1)"
fi

# --- ENSURE EVIDENCE DIRECTORY (safer permissions) ---
EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/britton_evidence}"
mkdir -p "$EVIDENCE_DIR"
# Make readable/executable by owner and group; not world-writable by default
chmod -R 0755 "$EVIDENCE_DIR" || true
echo "[INFO] Evidence directory ready: $EVIDENCE_DIR"

# --- NON-BLOCKING WARMUP DIAGNOSTICS CALL (background) ---
if command -v curl >/dev/null 2>&1; then
    # fire a delayed background curl to warm up app (won't block start)
    ( sleep 1 && curl -fsS "http://127.0.0.1:${PORT}/diagnostics/run" >/dev/null 2>&1 ) & disown || true
fi

# --- BUILD final gunicorn command if not provided ---
if [ -z "$GUNICORN_CMD" ]; then
    if [ -f "gunicorn_conf.py" ]; then
        GUNICORN_CMD="gunicorn -c gunicorn_conf.py main:app"
    else
        GUNICORN_CMD="gunicorn main:app"
    fi
fi

# Append dynamic flags if missing
case " $GUNICORN_CMD " in
  *" --workers "* ) true ;;
  *) GUNICORN_CMD="$GUNICORN_CMD --workers $GUNICORN_WORKERS" ;;
esac

case " $GUNICORN_CMD " in
  *" --threads "* ) true ;;
  *) GUNICORN_CMD="$GUNICORN_CMD --threads $GUNICORN_THREADS" ;;
esac

case " $GUNICORN_CMD " in
  *" --timeout "* ) true ;;
  *) GUNICORN_CMD="$GUNICORN_CMD --timeout $GUNICORN_TIMEOUT" ;;
esac

case " $GUNICORN_CMD " in
  *" --bind "* ) true ;;
  *) GUNICORN_CMD="$GUNICORN_CMD --bind 0.0.0.0:${PORT}" ;;
esac

echo "[INFO] Starting Gunicorn server: $GUNICORN_CMD"
# Use eval so any quoted args in $GUNICORN_CMD are respected; then exec to replace shell
eval "exec $GUNICORN_CMD"
