#!/usr/bin/env bash
set -euo pipefail

# entrypoint.sh — start tasks & start Gunicorn with dynamic worker tuning
# - Runs DB migrations if alembic present (optional)
# - Installs Playwright browsers at runtime if INSTALL_PLAYWRIGHT=1
# - Computes gunicorn worker count by CPU and starts Gunicorn
# - Uses gunicorn_conf.py by default (production-ready config)

# Configurable via env:
#  - PORT (default 10000)
#  - GUNICORN_WORKERS, GUNICORN_THREADS, GUNICORN_TIMEOUT
#  - INSTALL_PLAYWRIGHT=1 to ensure playwright browsers (safe no-op if already installed)
#  - RUN_MIGRATIONS=1 to run alembic upgrade head if alembic present
#  - VENV_PATH to activate a virtualenv (optional)
#  - GUNICORN_CMD override to customize final command (optional)

PORT="${PORT:-10000}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-1}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-0}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
GUNICORN_CMD="${GUNICORN_CMD:-}"

# Activate virtualenv if present
if [ -d "${VENV_PATH}" ] && [ -f "${VENV_PATH}/bin/activate" ]; then
  # shellcheck disable=SC1090
  source "${VENV_PATH}/bin/activate"
  echo "Activated virtualenv at ${VENV_PATH}"
else
  echo "No virtualenv found at ${VENV_PATH} — continuing without activation"
fi

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

# Safety caps (prevent runaway worker counts on tiny or very large hosts)
MAX_WORKERS_CAP="${MAX_WORKERS_CAP:-32}"
if [ "${WORKERS}" -gt "${MAX_WORKERS_CAP}" ]; then
  echo "Capping workers ${WORKERS} -> ${MAX_WORKERS_CAP}"
  WORKERS="${MAX_WORKERS_CAP}"
fi

echo "Entrypoint: CPU_COUNT=${CPU_COUNT}, using WORKERS=${WORKERS}, THREADS=${THREADS}, TIMEOUT=${TIMEOUT}"

# Helper: wait for TCP service (host:port) up to timeout seconds
wait_for_tcp() {
  local hostport=$1
  local timeout=${2:-30}
  if [ -z "$hostport" ]; then return 0; fi
  IFS=':' read -r host port <<<"$hostport"
  echo "Waiting for $host:$port (timeout ${timeout}s)..."
  for i in $(seq 1 "${timeout}"); do
    if nc -z "$host" "$port" >/dev/null 2>&1; then
      echo "$host:$port is available"
      return 0
    fi
    sleep 1
  done
  echo "Warning: $host:$port did not become available within ${timeout}s"
  return 1
}

# Optional: Wait for Redis / Postgres if REDIS_URL / DATABASE_URL are set and use tcp host:port format
# (Only attempt if nc is available)
if command -v nc >/dev/null 2>&1; then
  if [ -n "${REDIS_URL:-}" ]; then
    # try to parse host:port from typical redis url formats
    REDIS_HOSTPORT=$(echo "${REDIS_URL}" | sed -E 's#^redis://([^/@]+@)?([^:]+:[0-9]+).*#\2#; t; s#.*##')
    [ -n "$REDIS_HOSTPORT" ] && wait_for_tcp "${REDIS_HOSTPORT}" 15 || true
  fi
  if [ -n "${DATABASE_URL:-}" ]; then
    # try to parse host:port from postgres-like URLs: postgres://user:pass@host:port/db
    DB_HOSTPORT=$(echo "${DATABASE_URL}" | sed -E 's#^.*@([^:/]+:[0-9]+).*#\1#; t; s#.*##')
    [ -n "$DB_HOSTPORT" ] && wait_for_tcp "${DB_HOSTPORT}" 20 || true
  fi
else
  echo "nc (netcat) not available; skipping TCP wait checks"
fi

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
    # use --with-deps to avoid missing system libs in some images
    python -m playwright install --with-deps chromium || echo "Playwright browser install failed (continuing)"
  else
    echo "Playwright package not present in environment; skipping browser install."
  fi
fi

# Ensure evidence dir exists & permissions
EVIDENCE_DIR="${EVIDENCE_DIR:-/tmp/britton_evidence}"
mkdir -p "${EVIDENCE_DIR}"
chmod -R a+rwx "${EVIDENCE_DIR}" || true

# Warm up step (non-blocking): try a local diagnostics call if app is already listening (rare)
# This is safest as a best-effort and will not block startup
if command -v curl >/dev/null 2>&1; then
  ( sleep 0.5 && curl -fsS "http://127.0.0.1:${PORT}/diagnostics/run" >/dev/null 2>&1 ) & disown || true
fi

# Default gunicorn command uses gunicorn_conf.py if present
if [ -z "${GUNICORN_CMD}" ]; then
  if [ -f "gunicorn_conf.py" ]; then
    GUNICORN_CMD="gunicorn -c gunicorn_conf.py main:app"
  else
    GUNICORN_CMD="gunicorn main:app"
  fi
fi

# Append computed worker/thread/timeout flags to command if not already specified
# Only append if they don't appear in GUNICORN_CMD string to avoid duplicate flags
if ! echo "${GUNICORN_CMD}" | grep -q -- '--workers'; then
  GUNICORN_CMD="${GUNICORN_CMD} --workers ${WORKERS}"
fi
if ! echo "${GUNICORN_CMD}" | grep -q -- '--threads'; then
  GUNICORN_CMD="${GUNICORN_CMD} --threads ${THREADS}"
fi
if ! echo "${GUNICORN_CMD}" | grep -q -- '--timeout'; then
  GUNICORN_CMD="${GUNICORN_CMD} --timeout ${TIMEOUT}"
fi
# Bind to port if not present
if ! echo "${GUNICORN_CMD}" | grep -q -- '--bind'; then
  GUNICORN_CMD="${GUNICORN_CMD} --bind 0.0.0.0:${PORT}"
fi

echo "Starting server with: ${GUNICORN_CMD}"
# exec replaces the shell with gunicorn process so signals are handled correctly
exec ${GUNICORN_CMD}
