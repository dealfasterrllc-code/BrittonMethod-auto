#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# BrittonMethod-auto — entrypoint.sh
# World-class production-ready startup script
###############################################################################

# --- CONFIG (overridable by env / Render) ---
PORT="${1:-${PORT:-8000}}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-0}"
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-0}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
CPU_COUNT="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
RECOMMENDED_WORKERS=$(( CPU_COUNT*2 + 1 ))
GUNICORN_WORKERS="${GUNICORN_WORKERS:-$RECOMMENDED_WORKERS}"
GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
MAX_WORKERS_CAP="${MAX_WORKERS_CAP:-8}"

# Cap workers to prevent runaway
if [ "${GUNICORN_WORKERS}" -gt "${MAX_WORKERS_CAP}" ]; then
    echo "[WARN] Worker count ${GUNICORN_WORKERS} exceeds cap ${MAX_WORKERS_CAP}, reducing"
    GUNICORN_WORKERS="${MAX_WORKERS_CAP}"
fi

echo "[INFO] Config — CPU: ${CPU_COUNT}, WORKERS: ${GUNICORN_WORKERS}, THREADS: ${GUNICORN_THREADS}, TIMEOUT: ${GUNICORN_TIMEOUT}, PORT: ${PORT}"

# --- ACTIVATE VIRTUALENV ---
if [ -d "${VENV_PATH}" ] && [ -f "${VENV_PATH}/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "${VENV_PATH}/bin/activate"
    echo "[INFO] Activated virtualenv at ${VENV_PATH}"
else
    echo "[WARN] Virtualenv not found at ${VENV_PATH}, continuing without it"
fi

# --- EVIDENCE DIRECTORY ---
EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/britton_evidence}"
mkdir -p "$EVIDENCE_DIR"
chmod -R 0755 "$EVIDENCE_DIR"
echo "[INFO] Evidence directory ready: $EVIDENCE_DIR"

# --- OPTIONAL ALEMBIC MIGRATIONS ---
if [ "${RUN_MIGRATIONS}" = "1" ] && command -v alembic >/dev/null 2>&1 && [ -f "alembic.ini" ]; then
    echo "[INFO] Running Alembic migrations..."
    alembic upgrade head || echo "[WARN] Alembic migration failed, continuing"
else
    echo "[INFO] Skipping Alembic migrations"
fi

# --- OPTIONAL PLAYWRIGHT INSTALL ---
if [ "${INSTALL_PLAYWRIGHT}" = "1" ] && [ "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD}" != "1" ]; then
    if python -c "import playwright" >/dev/null 2>&1; then
        echo "[INFO] Installing Playwright browsers..."
        python -m playwright install chromium || echo "[WARN] Playwright browser install failed"
    else
        echo "[INFO] Playwright package not installed, skipping browser install"
    fi
else
    echo "[INFO] Playwright install skipped"
fi

# --- TCP DEPENDENCY CHECKS ---
wait_for_tcp() {
    local hostport=$1
    local timeout=${2:-30}
    [ -z "$hostport" ] && return 0
    IFS=':' read -r host port <<<"$hostport"
    [ -z "$host" ] || [ -z "$port" ] && return 1
    echo "[INFO] Waiting for $host:$port (timeout ${timeout}s)..."
    for i in $(seq 1 "$timeout"); do
        nc -z "$host" "$port" >/dev/null 2>&1 && echo "[INFO] $host:$port is up" && return 0
        sleep 1
    done
    echo "[WARN] $host:$port did not respond in ${timeout}s"
    return 1
}

command -v nc >/dev/null 2>&1 && {
    [ -n "${REDIS_URL:-}" ] && wait_for_tcp "$(echo "$REDIS_URL" | sed -E 's#^redis://([^/@]+@)?([^:]+:[0-9]+).*#\2#')" 15 || true
    [ -n "${DATABASE_URL:-}" ] && wait_for_tcp "$(echo "$DATABASE_URL" | sed -E 's#^.*@([^:/]+:[0-9]+).*#\1#')" 20 || true
} || echo "[WARN] nc not found; skipping TCP checks"

# --- NON-BLOCKING DIAGNOSTICS (optional) ---
command -v curl >/dev/null 2>&1 && ( sleep 1 && curl -fsS "http://127.0.0.1:${PORT}/diagnostics/run" >/dev/null 2>&1 ) & disown || true

# --- BUILD AND RUN GUNICORN ---
GUNICORN_CMD="gunicorn main:app --workers ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} --bind 0.0.0.0:${PORT}"
[ -f "gunicorn_conf.py" ] && GUNICORN_CMD="gunicorn -c gunicorn_conf.py main:app"

echo "[INFO] Starting Gunicorn server..."
exec $GUNICORN_CMD
