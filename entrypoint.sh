#!/usr/bin/env bash
set -euo pipefail

echo "[INFO] Entrypoint starting..."
# Optional: Run database migrations or init tasks here
if [[ -f "migrate.sh" ]]; then
  ./migrate.sh
fi

echo "[INFO] Launching Gunicorn..."
exec gunicorn -c gunicorn_conf.py main:app
