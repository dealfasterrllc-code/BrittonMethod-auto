#!/usr/bin/env bash
set -euo pipefail

# Entrypoint for BrittonMethod-auto
echo "[INFO] Starting entrypoint.sh..."

# Optional: initialize environment
export PATH="$HOME/.local/bin:$PATH"

# Ensure evidence dir exists
mkdir -p "${EVIDENCE_DIR:-/tmp/britton_evidence}"

# Start Gunicorn with robust config
exec gunicorn -c gunicorn_conf.py main:app
