#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# BrittonMethod-auto — entrypoint.sh
# Production-ready startup script for Render (or any containerized Python service)
# Handles virtualenv activation, Gunicorn startup, Playwright install, Alembic
# migrations, TCP dependency checks, logging, and safe defaults for memory.
###############################################################################

# --- CONFIG (can be overridden by env / Render) ---
PORT="${1:-${PORT:-8000}}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-0}"
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-0}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
GUNICORN_CMD="${GUNICORN_CMD:-}"
CPU_COUNT="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
RECOMMENDED_WORKERS=$(( CPU_COUNT*2 + 1 ))
GUNICORN_WORKERS="${GUNICORN_WORKERS:-$RECOMMENDED_WORKERS}"
GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
MAX_WORKERS_CAP="${MAX_WORKERS_CAP:-8}"

# --- ACTIVATE VIRTUALENV ---
if [ -d "${VENV_PATH}" ] && [ -f "${VENV_PATH}/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "${VENV_PATH}/bin/activate"
    echo "[INFO] Activated virtualenv at ${VENV_PATH}"
else
    echo "[WARN] No virtualenv found at ${VENV_PATH} — continuing without activation"
fi

# --- ENFORCE WORKER CAPS ---
if [ "${GUNICORN_WORKERS}" -gt "${MAX_WORKERS_CAP}" ]; then
    echo "[WARN] Worker count ${GUNICORN_WORKERS} exceeds cap ${MAX_WORKERS_CAP}, reducing"
    GUNICORN_WORKERS="${MAX_WORKERS_CAP}"
fi

echo "[INFO] Gunicorn config — CPU: ${CPU_COUNT}, WORKERS: ${GUNICORN_WORKERS}, THREADS: ${GUNICORN_THREADS}, TIMEOUT: ${GUNICORN_TIMEOUT}, PORT: ${PORT}"

# --- HELPER: WAIT FOR TCP DEPENDENCIES ---
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
    [ -n "${REDIS_URL:-}" ] && REDIS_HOSTPORT=$(echo "${REDIS_URL}" | sed -E 's#^redis://([^/@]+@)?([^:]+:[0-9]+).*#\2#') && [ -n "$REDIS_HOSTPORT" ] && wait_for_tcp "$REDIS_HOSTPORT" 15 || true
    [ -n "${DATABASE_URL:-}" ] && DB_HOSTPORT=$(echo "${DATABASE_URL}" | sed -E 's#^.*@([^:/]+:[0-9]+).*#\1#') && [ -n "$DB_HOSTPORT" ] && wait_for_tcp "$DB_HOSTPORT" 20 || true
else
    echo "[WARN] nc not available; skipping TCP wait checks"
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

# --- OPTIONAL PLAYWRIGHT INSTALL ---
if [ "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD}" = "1" ]; then
    echo "[INFO] PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 set; skipping Playwright browser install"
elif [ "${INSTALL_PLAYWRIGHT}" = "1" ]; then
    if python -c "import playwright" >/dev/null 2>&1; then
        echo "[INFO] Playwright detected — attempting browser install..."
        # Non-root safe browser install
        if ! python -m playwright install chromium; then
            echo "[WARN] Playwright browser install failed; continuing without browsers"
        fi
    else
        echo "[INFO] Playwright not installed; skipping browser install"
    fi
else
    echo "[INFO] Playwright install disabled (INSTALL_PLAYWRIGHT != 1)"
fi

# --- EVIDENCE DIRECTORY ---
EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/britton_evidence}"
mkdir -p "$EVIDENCE_DIR"
chmod -R 0755 "$EVIDENCE_DIR"
echo "[INFO] Evidence directory ready: $EVIDENCE_DIR"

# --- NON-BLOCKING WARMUP DIAGNOSTICS ---
if command -v curl >/dev/null 2>&1; then
    ( sleep 1 && curl -fsS "http://127.0.0.1:${PORT}/diagnostics/run" >/dev/null 2>&1 ) & disown || true
fi

# --- BUILD FINAL GUNICORN COMMAND ---
if [ -z "$GUNICORN_CMD" ]; then
    if [ -f "gunicorn_conf.py" ]; then
        GUNICORN_CMD="gunicorn -c gunicorn_conf.py main:app"
    else
        GUNICORN_CMD="gunicorn main:app"
    fi
fi

# Append dynamic flags if missing
case " $GUNICORN_CMD " in *" --workers "* ) ;; *) GUNICORN_CMD="$GUNICORN_CMD --workers $GUNICORN_WORKERS" ;; esac
case " $GUNICORN_CMD " in *" --threads "* ) ;; *) GUNICORN_CMD="$GUNICORN_CMD --threads $GUNICORN_THREADS" ;; esac
case " $GUNICORN_CMD " in *" --timeout "* ) ;; *) GUNICORN_CMD="$GUNICORN_CMD --timeout $GUNICORN_TIMEOUT" ;; esac
case " $GUNICORN_CMD " in *" --bind "* ) ;; *) GUNICORN_CMD="$GUNICORN_CMD --bind 0.0.0.0:${PORT}" ;; esac

echo "[INFO] Starting Gunicorn server: $GUNICORN_CMD"
exec eval "$GUNICORN_CMD"
