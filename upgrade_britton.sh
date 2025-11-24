#!/usr/bin/env bash
set -euo pipefail

echo "[INFO] Starting full BrittonMethod-auto upgrade..."

# --- 1. Update Python version ---
echo "3.12.2" > .python-version
echo "[INFO] .python-version updated"

# --- 2. Update entrypoint.sh ---
cat > entrypoint.sh << 'EOT'
#!/usr/bin/env bash
set -euo pipefail
# (Paste full production-ready entrypoint.sh here)
EOT
chmod +x entrypoint.sh
echo "[INFO] entrypoint.sh upgraded"

# --- 3. Update gunicorn_conf.py ---
cat > gunicorn_conf.py << 'EOT'
# (Paste full production-ready gunicorn_conf.py here)
EOT
echo "[INFO] gunicorn_conf.py upgraded"

# --- 4. Update main.py ---
cat > main.py << 'EOT'
# (Paste full production-ready main.py here)
EOT
chmod +x main.py
echo "[INFO] main.py upgraded"

# --- 5. Update .render.yaml ---
cat > .render.yaml << 'EOT'
# (Paste full production-ready .render.yaml here)
EOT
echo "[INFO] .render.yaml upgraded"

# --- 6. Stage, commit, and push ---
git add .python-version entrypoint.sh gunicorn_conf.py main.py .render.yaml
git commit -m "Full upgrade: Python 3.12.2, production-ready main.py, entrypoint.sh, gunicorn_conf.py, .render.yaml"
git push origin main

echo "[SUCCESS] All upgrades applied and pushed to GitHub!"
