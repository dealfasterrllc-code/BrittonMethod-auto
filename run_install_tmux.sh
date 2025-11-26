#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$HOME/BrittonMethod-auto"
VENV_DIR="$PROJECT_DIR/venv"
MIN_REQ="$PROJECT_DIR/requirements.minimal.txt"
LOGFILE="$PROJECT_DIR/install.log"
cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip setuptools wheel
# use prefer-binary to avoid costly builds
pip install --prefer-binary -r "$MIN_REQ" 2>&1 | tee "$LOGFILE"
echo "INSTALL_DONE"
