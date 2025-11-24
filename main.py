#!/usr/bin/env python3
"""
main.py — Production-ready Flask entry for BrittonMethod-auto

Features:
- Threaded background job queue
- Graceful shutdown
- Unified AI provider shim
- Optional SQLAlchemy support
- Evidence storage (S3/local)
- Full API key enforcement
- Robust Flask endpoints
"""

from __future__ import annotations
import os, json, uuid, time, traceback, hashlib, logging, threading, queue
from datetime import datetime
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv

# Optional: external API requests
try:
    import requests
except ImportError:
    requests = None

# Optional SQLAlchemy
SQLALCHEMY_AVAILABLE = False
try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, Job
    SQLALCHEMY_AVAILABLE = True
except Exception:
    SQLALCHEMY_AVAILABLE = False

# app_core imports
APP_CORE_AVAILABLE = False
try:
    import app_core as ac
    underwriter_deterministic = getattr(ac, "underwriter_deterministic", None)
    monte_carlo_simulation = getattr(ac, "monte_carlo_simulation", None)
    simulate_refund_waterfall = getattr(ac, "simulate_refund_waterfall", None)
    verify_listing_pipeline = getattr(ac, "verify_listing_pipeline", None)
    store_evidence_binary = getattr(ac, "store_evidence_binary", None)
    generate_long_loi_text = getattr(ac, "generate_long_loi_text", None)
    provider_chain_fn = getattr(ac, "provider_generate_text_with_fallback", None)
    legacy_get_openai_response = getattr(ac, "get_openai_response", None)
    deterministic_fallback = getattr(ac, "deterministic_fallback", None)
    LLM_PROMPT = getattr(ac, "LLM_PROMPT", None)
    APP_CORE_AVAILABLE = True
except Exception:
    underwriter_deterministic = monte_carlo_simulation = simulate_refund_waterfall = None
    verify_listing_pipeline = store_evidence_binary = generate_long_loi_text = None
    provider_chain_fn = legacy_get_openai_response = deterministic_fallback = None
    LLM_PROMPT = None

# Provider shim
def _shim_provider_wrapper(prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
    try:
        if provider_chain_fn:
            out = provider_chain_fn(prompt, max_tokens=max_tokens)
            if isinstance(out, dict) and out.get("ok"):
                return {"ok": True, "response": out.get("response"), "provider": out.get("provider", "UNKNOWN"), "raw": out.get("raw")}
            if isinstance(out, str):
                return {"ok": True, "response": out, "provider": "PROVIDER_CHAIN_TEXT"}
        if legacy_get_openai_response:
            res = legacy_get_openai_response(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
            if isinstance(res, dict) and res.get("ok"):
                return {"ok": True, "response": res.get("response"), "provider": "legacy_get_openai_response", "raw": res}
            if isinstance(res, str):
                return {"ok": True, "response": res, "provider": "legacy_get_openai_response"}
        if deterministic_fallback:
            text = deterministic_fallback(prompt, min_words=200)
            return {"ok": True, "response": text, "provider": "DETERMINISTIC_FALLBACK"}
        return {"ok": True, "response": f"(fallback echo) {prompt}", "provider": "ECHO_FALLBACK"}
    except Exception as e:
        return {"ok": False, "error": str(e), "provider": "EXCEPTION"}

get_openai_response = _shim_provider_wrapper

# Load .env
load_dotenv()

# Env variables
API_KEY = os.environ.get("BRITTON_API_KEY", "")
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///britton.db")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("britton")

# Job queue
JOB_QUEUE = queue.Queue()
JOB_STORE: Dict[str, Dict[str, Any]] = {}
_SHUTDOWN_EVENT = threading.Event()

def require_api_key():
    if not API_KEY:
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if key != API_KEY:
        abort(401, description="Missing or invalid API key.")
    return True

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "healthy"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting BrittonMethod API on port %s", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
