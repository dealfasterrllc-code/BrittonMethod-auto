#!/usr/bin/env bash
set -euo pipefail

# -------------------------------------------------------------
# create_scaffold.sh
# Creates scaffold files for BrittonMethod-auto
#
# Usage:
#   ./create_scaffold.sh            # creates files, commits locally, DOES NOT push
#   DO_PUSH=true ./create_scaffold.sh   # creates files, commits, and pushes to origin/main
#   GIT_BRANCH=dev ./create_scaffold.sh # commit to different branch (create branch first)
#
# IMPORTANT: Inspect generated files before pushing. Do not commit secrets.
# -------------------------------------------------------------

# --- Configuration (edit if needed) ---
GIT_BRANCH="${GIT_BRANCH:-main}"
COMMIT_MSG="${COMMIT_MSG:-chore: add scaffold (config, core/logger, modules/ingestion, evidence ledger, workers, tests)}"
DO_PUSH="${DO_PUSH:-false}"   # set DO_PUSH=true to push to origin
# -------------------------------

echo
echo "BRITTON METHOD — Scaffold creator"
echo "Branch: ${GIT_BRANCH}"
echo "Commit message: ${COMMIT_MSG}"
echo "Will push? ${DO_PUSH}"
echo

# ensure we're in a git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: This directory is not a git repository. Clone your repo and re-run this script."
  exit 1
fi

# create directories
mkdir -p config core modules modules/api_wrappers evidence workers tests/unit

# -------------------------
# 1) config/config_example.py
# -------------------------
cat > config/config_example.py <<'PY'
"""
config_example.py

Copy this file to config.py and fill secrets & vendor keys.
Do NOT commit real secrets to git.
"""
from os import environ

# Database
DATABASE_URL = environ.get("DATABASE_URL", "sqlite:///./data/listings.db")

# Monte Carlo
MONTE_CARLO_RUNS = int(environ.get("MONTE_CARLO_RUNS", "1000"))

# Storage
USE_S3 = environ.get("USE_S3", "false").lower() in ("1","true","yes")
S3_BUCKET = environ.get("S3_BUCKET", "britton-evidence")
S3_REGION = environ.get("S3_REGION", "us-east-1")

# Vendor credentials (set via Render / CI secrets)
ATTOM_API_KEY = environ.get("ATTOM_API_KEY", "")
TRESTLE_API_KEY = environ.get("TRESTLE_API_KEY", "")
TITLE_PROVIDER_API_KEY = environ.get("TITLE_PROVIDER_API_KEY", "")
TWILIO_SID = environ.get("TWILIO_SID", "")
TWILIO_TOKEN = environ.get("TWILIO_TOKEN", "")

# App
SECRET_KEY = environ.get("SECRET_KEY", "change-me-in-production")

# Thresholds
BRITTON_OFFER_THRESHOLD = float(environ.get("BRITTON_OFFER_THRESHOLD", "70"))
CONFIDENCE_THRESHOLD = float(environ.get("CONFIDENCE_THRESHOLD", "0.7"))
PY

# -------------------------
# 2) core/logger.py
# -------------------------
cat > core/logger.py <<'PY'
"""
core/logger.py

Structured JSON-like logger helper used across modules.
"""
import logging
import json
import sys
from datetime import datetime

class JsonLogger:
    def __init__(self, name=__name__, level=logging.INFO):
        self.logger = logging.getLogger(name)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.handlers = []
        self.logger.addHandler(handler)
        self.logger.setLevel(level)

    def _format(self, level, message, **kwargs):
        base = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "msg": message
        }
        base.update(kwargs)
        return json.dumps(base, default=str)

    def info(self, msg, **kwargs):
        self.logger.info(self._format("INFO", msg, **kwargs))

    def warn(self, msg, **kwargs):
        self.logger.warning(self._format("WARN", msg, **kwargs))

    def error(self, msg, **kwargs):
        self.logger.error(self._format("ERROR", msg, **kwargs))

    def debug(self, msg, **kwargs):
        self.logger.debug(self._format("DEBUG", msg, **kwargs))

# convenience
logger = JsonLogger("britton")
PY

# -------------------------
# 3) modules/ingestion.py
# -------------------------
cat > modules/ingestion.py <<'PY'
"""
modules/ingestion.py

Small ingestion utilities: best-effort public scrape / parse helper.
This is intentionally minimal and safe — do not rely on scraping portals that forbid it.
Production: replace with licensed vendor connectors.
"""
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any

def tiny_scrape(url: str, timeout: int = 8) -> Dict[str, Any]:
    """
    Best-effort GET + parse for public pages.
    Returns dict: title, price_guess, snippet, raw_html
    """
    try:
        headers = {"User-Agent": "BrittonMethodBot/1.0 (+https://dealfasterr.io)"}
        r = requests.get(url, timeout=timeout, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.title.string or "").strip() if soup.title else ""
        full_text = soup.get_text(" ", strip=True)
        # price guess (very naive) - looks for $nnn patterns
        import re
        m = re.search(r"\$\s?([\d,]{4,})", full_text)
        price = int(m.group(1).replace(",", "")) if m else None
        snippet = full_text[:1000]
        return {"title": title, "price": price, "snippet": snippet, "html": r.text}
    except Exception as e:
        return {"error": str(e), "url": url}

def parse_listing_from_text(text: str):
    """
    Very lightweight parser to extract fields from free-form listing text.
    Production: replace with robust NLP + vendor fields.
    """
    out = {}
    if not text:
        return out
    import re
    m_price = re.search(r"\$\s?([\d,]{4,})", text)
    if m_price:
        out["price"] = int(m_price.group(1).replace(",", ""))
    m_units = re.search(r"(\d+)\s*(units|unit|bedrooms|beds|beds\.)", text, re.I)
    if m_units:
        out["units"] = int(m_units.group(1))
    return out
PY

# -------------------------
# 4) evidence/ledger.py
# -------------------------
cat > evidence/ledger.py <<'PY'
"""
evidence/ledger.py

Helpers to create evidence items, compute SHA-256, and save locally.
In production upload raw payloads to S3 (boto3) and store signed URLs.
"""
import os
import json
import hashlib
from datetime import datetime
from typing import Dict, Any

EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def build_evidence_item(source: str, raw_bytes: bytes, meta: Dict[str, Any] = None) -> Dict[str, Any]:
    meta = meta or {}
    h = sha256_bytes(raw_bytes)
    filename = f"{h}.bin"
    local_path = os.path.join(EVIDENCE_DIR, filename)
    with open(local_path, "wb") as f:
        f.write(raw_bytes)
    item = {
        "id": h,
        "source": source,
        "sha256": h,
        "size": len(raw_bytes),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "local_path": local_path,
        "meta": meta
    }
    return item

def manifest_from_items(items):
    """
    Given a list of evidence items, return a manifest (json-ready).
    """
    return {"count": len(items), "items": items, "generated_at": datetime.utcnow().isoformat() + "Z"}
PY

# -------------------------
# 5) workers/tasks.py
# -------------------------
cat > workers/tasks.py <<'PY'
"""
workers/tasks.py

Simple async enrichment skeleton. Replace stubs with vendor API calls.
This can later be adapted to Celery/RQ.
"""
import asyncio
from typing import List, Dict, Any
from modules.ingestion import tiny_scrape
from evidence.ledger import build_evidence_item
import json

async def call_assessor_stub(address: str):
    # Simulate a quick assessor call
    await asyncio.sleep(0.15)
    payload = {"source": "assessor_stub", "address": address, "assessed_value": 320000}
    raw = json.dumps(payload).encode("utf-8")
    return build_evidence_item("assessor_stub", raw, {"address": address})

async def call_mls_stub(address: str):
    await asyncio.sleep(0.2)
    payload = {"source": "mls_stub", "address": address, "status": "active", "price": 399000}
    raw = json.dumps(payload).encode("utf-8")
    return build_evidence_item("mls_stub", raw, {"address": address})

async def enrich_listing_async(listing_id: str, address: str, depth: str = "QUICK") -> List[Dict[str, Any]]:
    tasks = []
    if depth == "QUICK":
        tasks = [call_assessor_stub(address)]
    elif depth == "FULL":
        tasks = [call_assessor_stub(address), call_mls_stub(address)]
    else:
        tasks = [call_assessor_stub(address), call_mls_stub(address)]
    results = await asyncio.gather(*tasks)
    return results

def enrich_listing(listing_id: str, address: str, depth: str = "QUICK"):
    return asyncio.run(enrich_listing_async(listing_id, address, depth))
PY

# -------------------------
# 6) tests/unit/test_evidence.py
# -------------------------
cat > tests/unit/test_evidence.py <<'PY'
import os
from evidence.ledger import build_evidence_item, sha256_bytes, manifest_from_items

def test_build_evidence_item_roundtrip(tmp_path):
    data = b"hello evidence"
    item = build_evidence_item("test_source", data, {"note": "unit test"})
    assert item["sha256"] == sha256_bytes(data)
    assert "local_path" in item
    assert os.path.exists(item["local_path"]) is True

def test_manifest_from_items(tmp_path):
    data1 = b"a"
    data2 = b"b"
    it1 = build_evidence_item("s1", data1)
    it2 = build_evidence_item("s2", data2)
    m = manifest_from_items([it1, it2])
    assert m["count"] == 2
    assert "generated_at" in m
PY

# -------------------------
# 7) update .gitignore
# -------------------------
if [ ! -f .gitignore ]; then
  cat > .gitignore <<'GI'
__pycache__/
*.pyc
.env
/tmp/
data/*.db
EVIDENCE_DIR/
GI
else
  # avoid duplicate entries
  grep -q "__pycache__/" .gitignore || echo "__pycache__/" >> .gitignore
  grep -q "*.pyc" .gitignore || echo "*.pyc" >> .gitignore
  grep -q ".env" .gitignore || echo ".env" >> .gitignore
  grep -q "data/*.db" .gitignore || echo "data/*.db" >> .gitignore
  grep -q "EVIDENCE_DIR/" .gitignore || echo "EVIDENCE_DIR/" >> .gitignore
fi

# -------------------------
# Git add / commit / optional push
# -------------------------
git add config core modules evidence workers tests .gitignore || true

# create branch if not on it
current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "$GIT_BRANCH" ]; then
  echo "Creating and switching to branch: $GIT_BRANCH"
  git checkout -b "$GIT_BRANCH"
fi

git commit -m "$COMMIT_MSG" || echo "No changes to commit"

if [ "$DO_PUSH" = "true" ] || [ "$DO_PUSH" = "1" ]; then
  echo "Pushing to origin/$GIT_BRANCH..."
  git push origin "$GIT_BRANCH"
else
  echo "DO_PUSH is false. Not pushing. Inspect changes and push when ready."
fi

# -------------------------
# Summary
# -------------------------
echo
echo "Scaffold created. Files:"
echo " - config/config_example.py"
echo " - core/logger.py"
echo " - modules/ingestion.py"
echo " - evidence/ledger.py"
echo " - workers/tasks.py"
echo " - tests/unit/test_evidence.py"
echo
echo "To run tests locally:"
echo "  python -m venv .venv && source .venv/bin/activate"
echo "  pip install -r requirements.txt"
echo "  pytest -q"
echo
echo "If you want me to also add Dockerfile, CI, or additional modules (enrichment, S3, Celery), say which (Docker/CI/S3/Celery)."
echo
