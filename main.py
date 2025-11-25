#!/usr/bin/env python3
"""
main.py — World-class, production-ready Flask entry for BrittonMethod-auto

This file is a full, final, end-to-end implementation that:
 - Provides a secure API (optional API key) and endpoints for prompts, LOI generation,
   job enqueueing (analyze / verify / simulate), job lookup and list.
 - Wires to app_core when available (preserves original logic & deterministic fallback).
 - Attempts to use a provider factory (providers.factory) if present to make Gemini primary.
 - Uses a background worker thread and optional SQLAlchemy persistence.
 - Stores evidence locally or to S3 (if configured).
 - Designed to run under Gunicorn (recommended) or direct Flask dev server.
"""

from __future__ import annotations
import os
import sys
import json
import uuid
import time
import atexit
import signal
import logging
import traceback
import hashlib
import threading
import queue
from datetime import datetime
from typing import Dict, Any, Optional, List

from flask import Flask, request, jsonify, abort, Response
from dotenv import load_dotenv

# load .env if present
load_dotenv()

# -------------------------
# CONFIG
# -------------------------
API_KEY = os.environ.get("BRITTON_API_KEY", "").strip()
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "us-east-1"))
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///britton.db")
REDIS_URL = os.environ.get("REDIS_URL", "")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))
HUMAN_IN_LOOP_VALUE = float(os.environ.get("HUMAN_IN_LOOP_VALUE", "50000000"))
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "10"))
MODEL_PROVIDER_PRIMARY = os.environ.get("MODEL_PROVIDER_PRIMARY", "GEMINI").upper()
PORT = int(os.environ.get("PORT", 8000))

os.makedirs(EVIDENCE_DIR, exist_ok=True)

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    level=os.environ.get("APP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("britton.main")

# -------------------------
# Optional SQLAlchemy persistence
# -------------------------
SQLALCHEMY_AVAILABLE = False
SessionLocal = None
JobModel = None
try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base as ModelsBase, Job as JobModel  # models.py should define Base and Job
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
    SessionLocal = sessionmaker(bind=engine)
    try:
        if ModelsBase is not None:
            ModelsBase.metadata.create_all(bind=engine)
            logger.info("SQLAlchemy metadata created.")
    except Exception:
        logger.exception("Failed to create SQLAlchemy metadata.")
    SQLALCHEMY_AVAILABLE = True
except Exception:
    logger.debug("SQLAlchemy not available or models.py missing; continuing without DB persistence.", exc_info=True)

# -------------------------
# app_core integration (preserve your logic)
# -------------------------
APP_CORE_AVAILABLE = False
_underwriter_deterministic = None
_monte_carlo_simulation = None
_simulate_refund_waterfall = None
_verify_listing_pipeline = None
_store_evidence_binary_acore = None
_generate_long_loi_text = None
_provider_generate_text_with_fallback = None
_legacy_get_openai_response = None
_deterministic_fallback = None
_LLM_PROMPT = None

try:
    import app_core as ac  # use the app_core you provided earlier
    _underwriter_deterministic = getattr(ac, "underwriter_deterministic", None)
    _monte_carlo_simulation = getattr(ac, "monte_carlo_simulation", None)
    _simulate_refund_waterfall = getattr(ac, "simulate_refund_waterfall", None)
    _verify_listing_pipeline = getattr(ac, "verify_listing_pipeline", None)
    _store_evidence_binary_acore = getattr(ac, "store_evidence_binary", None)
    _generate_long_loi_text = getattr(ac, "generate_long_loi_text", None)
    _provider_generate_text_with_fallback = getattr(ac, "provider_generate_text_with_fallback", None)
    _legacy_get_openai_response = getattr(ac, "get_openai_response", None)
    _deterministic_fallback = getattr(ac, "deterministic_fallback", None)
    _LLM_PROMPT = getattr(ac, "LLM_PROMPT", None)
    APP_CORE_AVAILABLE = True
    logger.info("Loaded app_core functions from app_core.py")
except Exception:
    logger.debug("app_core not available; will rely on provider factory or deterministic fallback.", exc_info=True)

# -------------------------
# Optional providers.factory (recommended)
# -------------------------
PROVIDER_FACTORY = None
try:
    import providers.factory as provider_factory
    PROVIDER_FACTORY = provider_factory
    logger.info("providers.factory loaded (will use provider chain). Primary provider: %s", MODEL_PROVIDER_PRIMARY)
except Exception:
    logger.debug("providers.factory not found; falling back to app_core provider shim or deterministic fallback.")

# -------------------------
# Optional boto3 (S3)
# -------------------------
S3_CLIENT = None
if USE_S3:
    try:
        import boto3
        S3_CLIENT = boto3.client("s3", region_name=S3_REGION)
        logger.info("S3 client initialized (evidence uploads enabled).")
    except Exception:
        logger.exception("boto3 failed to import; continuing with local evidence storage.")
        S3_CLIENT = None

# -------------------------
# Safe HTTP client
# -------------------------
try:
    import requests
except Exception:
    requests = None

# -------------------------
# Utilities
# -------------------------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def mask_key(val: Optional[str]) -> str:
    if not val:
        return "MISSING"
    s = str(val)
    return s[:4] + "..." + s[-4:] if len(s) > 8 else s

def require_api_key():
    if not API_KEY:
        # dev-friendly: allow if API key not configured
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if not key or key != API_KEY:
        abort(Response("Missing or invalid API key", status=401))
    return True

def get_payload() -> Dict[str, Any]:
    """
    Read JSON payload or raw string safely.
    """
    p = request.get_json(silent=True)
    if p is None:
        raw = request.get_data(as_text=True).strip() or ""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"prompt": parsed}
        except Exception:
            return {"prompt": raw}
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        return {"prompt": p}
    return {"_raw": str(p)}

# -------------------------
# Evidence storage (local or S3). Use app_core's function if available to preserve manifest behavior.
# -------------------------
def _store_evidence_binary_local(kind: str, data: bytes, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = meta or {}
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"{kind}_{ts}_{uuid.uuid4().hex}.bin"
    try:
        if USE_S3 and S3_CLIENT and S3_BUCKET:
            key = f"evidence/{filename}"
            S3_CLIENT.put_object(Bucket=S3_BUCKET, Key=key, Body=data)
            url = f"s3://{S3_BUCKET}/{key}"
            logger.info("Evidence stored to S3: %s", url)
            return {"ok": True, "store": "s3", "path": url, "meta": meta}
        else:
            local_path = os.path.join(EVIDENCE_DIR, filename)
            with open(local_path, "wb") as f:
                f.write(data)
            logger.info("Evidence stored locally: %s", local_path)
            return {"ok": True, "store": "local", "path": local_path, "meta": meta}
    except Exception:
        logger.exception("Failed to store evidence")
        return {"ok": False, "error": "store_failed"}

# choose app_core implementation if provided
store_evidence_binary = _store_evidence_binary_acore or _store_evidence_binary_local

# -------------------------
# Provider shim (app_core or provider factory)
# -------------------------
def _shim_provider_from_factory(messages: List[Dict[str,str]], max_tokens: int = 1024, timeout: int = 15) -> Dict[str, Any]:
    """
    Use providers.factory.get_response if present.
    messages: list of {"role": "...", "content": "..."}
    Returns normalized dict {"ok": bool, "provider": str, "response": str, "raw": ...}
    """
    if PROVIDER_FACTORY:
        try:
            res = PROVIDER_FACTORY.get_response(messages, max_tokens=max_tokens, timeout=timeout)
            if isinstance(res, dict) and res.get("ok"):
                return res
            # normalize
            return {"ok": False, "provider": res.get("provider") if isinstance(res, dict) else None, "errors": res}
        except Exception:
            logger.exception("providers.factory raised an exception")
            return {"ok": False, "error": "factory_exception"}
    # fallback to app_core provider chain if available
    if _provider_generate_text_with_fallback:
        try:
            prompt = "\n".join([f"[{m.get('role','user')}]: {m.get('content','')}" for m in messages])
            out = _provider_generate_text_with_fallback(prompt, max_tokens=max_tokens)
            if out.get("ok"):
                return {"ok": True, "provider": out.get("provider", "CHAIN"), "response": out.get("response"), "raw": out.get("raw")}
            return {"ok": False, "error": out}
        except Exception:
            logger.exception("app_core provider chain failed")
            return {"ok": False, "error": "app_core_provider_exception"}
    # final deterministic fallback
    if _deterministic_fallback:
        try:
            prompt = "\n".join([m.get("content","") for m in messages])
            text = _deterministic_fallback(prompt, min_words=200)
            return {"ok": True, "provider": "DETERMINISTIC_FALLBACK", "response": text}
        except Exception:
            logger.exception("deterministic fallback failed")
            return {"ok": False, "error": "deterministic_failed"}
    # echo fallback
    return {"ok": True, "provider": "ECHO", "response": " ".join([m.get("content","") for m in messages])}

# alias for older compatibility
get_openai_response = lambda prompt, **kwargs: _shim_provider_from_factory([{"role":"user","content":prompt}], **kwargs)

# -------------------------
# Job Queue and Worker
# -------------------------
JOB_QUEUE: queue.Queue = queue.Queue()
JOB_STORE: Dict[str, Dict[str, Any]] = {}
_SHUTDOWN_EVENT = threading.Event()

def enqueue_job(job_type: str, payload: Dict[str, Any], run_monte_carlo: bool = True, mc_runs: int = 2000) -> str:
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {
        "id": job_id,
        "created": datetime.utcnow().isoformat() + "Z",
        "status": "queued",
        "type": job_type,
        "payload": payload,
    }
    JOB_QUEUE.put({"job_id": job_id, "type": job_type, "payload": payload, "monte_carlo": run_monte_carlo, "mc_runs": mc_runs})
    # persist to DB if available
    if SQLALCHEMY_AVAILABLE and SessionLocal and JobModel:
        try:
            db = SessionLocal()
            j = JobModel(id=job_id, payload=json.dumps(payload), status="queued")
            db.add(j)
            db.commit()
            db.close()
        except Exception:
            logger.exception("Failed to persist job to DB")
    return job_id

def _process_analyze(prop: Dict[str, Any], job_id: str, mc_runs: int = 2000) -> Dict[str, Any]:
    det = None
    mc = None
    try:
        if _underwriter_deterministic:
            det = _underwriter_deterministic(prop)
        else:
            # minimal deterministic compute if app_core not present
            det = {"price": float(prop.get("price", 0)), "confidence": float(prop.get("confidence", 0.5))}
        if _monte_carlo_simulation and mc_runs and mc_runs > 0:
            mc = _monte_carlo_simulation(prop, runs=mc_runs)
        # evidence
        raw = json.dumps({"property": prop, "det": det, "mc": mc}, ensure_ascii=False).encode("utf-8")
        ev = store_evidence_binary("analysis_bundle", raw, {"address": prop.get("address")})
        result = {"ok": True, "deal": det, "monte_carlo": mc, "evidence": ev}
        # escalation logic
        try:
            score = float(det.get("britton_score") if isinstance(det, dict) and det.get("britton_score") is not None else 0)
        except Exception:
            score = 0
        price = float(det.get("price") if isinstance(det, dict) and det.get("price") is not None else det.get("price", 0))
        confidence = float(det.get("confidence") if isinstance(det, dict) and det.get("confidence") is not None else 1.0)
        if score >= 95 or price >= HUMAN_IN_LOOP_VALUE or confidence < CONFIDENCE_THRESHOLD:
            result["_escalated"] = True
            JOB_STORE[job_id]["status"] = "escalated"
            JOB_STORE[job_id]["escalation_reason"] = "human_in_loop_threshold"
        else:
            JOB_STORE[job_id]["status"] = "done"
        JOB_STORE[job_id]["result"] = result
        return result
    except Exception:
        logger.exception("Error during analysis")
        JOB_STORE[job_id]["status"] = "error"
        JOB_STORE[job_id]["result"] = {"ok": False, "error": "analysis_failed", "tb": traceback.format_exc()}
        return JOB_STORE[job_id]["result"]

def _process_verify(manifest: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    try:
        if _verify_listing_pipeline:
            out = _verify_listing_pipeline(manifest, attempts=MAX_VERIFICATION_ATTEMPTS, require_checks=5)
        else:
            out = {"ok": False, "error": "verify_pipeline_missing"}
        JOB_STORE[job_id]["status"] = "done"
        JOB_STORE[job_id]["result"] = out
        return out
    except Exception:
        logger.exception("Error during verify")
        JOB_STORE[job_id]["status"] = "error"
        JOB_STORE[job_id]["result"] = {"ok": False, "error": "verify_failed", "tb": traceback.format_exc()}
        return JOB_STORE[job_id]["result"]

def _process_simulate(payload: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    try:
        if _simulate_refund_waterfall:
            sim = _simulate_refund_waterfall(float(payload.get("price", 0)), float(payload.get("existing_debt", 0)), float(payload.get("investor_equity_pct", 0.25)))
            out = {"ok": True, "simulation": sim}
        else:
            out = {"ok": False, "error": "simulate_missing"}
        JOB_STORE[job_id]["status"] = "done"
        JOB_STORE[job_id]["result"] = out
        return out
    except Exception:
        logger.exception("Error during simulate")
        JOB_STORE[job_id]["status"] = "error"
        JOB_STORE[job_id]["result"] = {"ok": False, "error": "simulate_failed", "tb": traceback.format_exc()}
        return JOB_STORE[job_id]["result"]

def worker_loop():
    logger.info("Background worker starting.")
    while not _SHUTDOWN_EVENT.is_set():
        try:
            job = JOB_QUEUE.get(timeout=1)
        except Exception:
            continue
        if job is None:
            break
        job_id = job.get("job_id")
        JOB_STORE[job_id]["status"] = "running"
        t0 = time.time()
        try:
            jtype = job.get("type")
            if jtype == "analyze":
                _process_analyze(job.get("payload", {}), job_id, mc_runs=job.get("mc_runs", 2000))
            elif jtype == "verify":
                _process_verify(job.get("payload", {}), job_id)
            elif jtype == "simulate":
                _process_simulate(job.get("payload", {}), job_id)
            else:
                logger.warning("Unknown job type: %s", jtype)
                JOB_STORE[job_id]["status"] = "error"
                JOB_STORE[job_id]["result"] = {"ok": False, "error": "unknown_job_type"}
            # persist job state to DB if possible
            if SQLALCHEMY_AVAILABLE and SessionLocal and JobModel:
                try:
                    db = SessionLocal()
                    j = db.query(JobModel).filter(JobModel.id == job_id).first()
                    if j:
                        j.status = JOB_STORE[job_id]["status"]
                        j.result = json.dumps(JOB_STORE[job_id].get("result")) if JOB_STORE[job_id].get("result") else None
                        db.commit()
                    db.close()
                except Exception:
                    logger.exception("Failed to update job in DB")
        except Exception:
            logger.exception("Worker loop exception")
            JOB_STORE[job_id]["status"] = "error"
            JOB_STORE[job_id]["result"] = {"ok": False, "error": "worker_exception", "tb": traceback.format_exc()}
        finally:
            JOB_STORE[job_id]["duration_seconds"] = time.time() - t0
            try:
                JOB_QUEUE.task_done()
            except Exception:
                pass
    logger.info("Background worker exiting.")

_worker_thread = threading.Thread(target=worker_loop, daemon=True, name="britton-worker")
_worker_thread.start()

# -------------------------
# Flask app & endpoints
# -------------------------
app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat() + "Z", "provider_factory": bool(PROVIDER_FACTORY), "app_core": APP_CORE_AVAILABLE})

@app.route("/diagnostics/run", methods=["GET"])
def diagnostics_run():
    try:
        diag = {
            "ok": True,
            "env": {
                "EVIDENCE_DIR_exists": os.path.isdir(EVIDENCE_DIR),
                "USE_S3": USE_S3,
                "S3_CONFIGURED": bool(S3_CLIENT),
                "SQLALCHEMY": SQLALCHEMY_AVAILABLE,
                "PROVIDER_FACTORY": bool(PROVIDER_FACTORY),
                "APP_CORE_AVAILABLE": APP_CORE_AVAILABLE,
                "MODEL_PROVIDER_PRIMARY": MODEL_PROVIDER_PRIMARY
            }
        }
        # include app_core diagnostics if available
        if APP_CORE_AVAILABLE and hasattr(ac, "diagnostics_report"):
            try:
                diag["app_core"] = ac.diagnostics_report()
            except Exception:
                diag["app_core_error"] = "diagnostics_report_failed"
        return jsonify(diag)
    except Exception:
        logger.exception("Diagnostics run failed")
        return jsonify({"ok": False, "error": "diagnostics_failed", "tb": traceback.format_exc()}), 500

@app.route("/prompt", methods=["POST"])
def api_prompt():
    # Accepts {"text": "..."} or {"messages": [{"role":"user","content":"..."}, ...]}
    payload = get_payload()
    text = payload.get("text")
    messages = payload.get("messages")
    if not messages:
        if not text:
            return jsonify({"ok": False, "error": "no_prompt_provided"}), 400
        messages = [{"role": "user", "content": text}]
    # optional API key
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    try:
        res = _shim_provider_from_factory(messages=messages, max_tokens=int(payload.get("max_tokens", 1024)))
        # store evidence for successful calls
        try:
            store_evidence_binary("prompt_request", json.dumps({"messages": messages}).encode("utf-8"), {"provider": res.get("provider")})
        except Exception:
            logger.debug("Failed storing prompt evidence")
        return jsonify(res)
    except Exception:
        logger.exception("Prompt handling failed")
        return jsonify({"ok": False, "error": "prompt_failed", "tb": traceback.format_exc()}), 500

@app.route("/generate/loi", methods=["POST"])
def api_generate_loi():
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    payload = get_payload()
    # Prefer using app_core.generate_long_loi_text if available (preserves LLM_PROMPT and persona behavior)
    try:
        if _generate_long_loi_text:
            # generate_long_loi_text expects a prop dict; preserve previous behavior
            prop = payload.get("property") or payload
            min_words = int(payload.get("min_words", 2000))
            max_words = int(payload.get("max_words", 2500))
            loi_text = _generate_long_loi_text(prop, min_words=min_words, max_words=max_words)
            # _generate_long_loi_text may return str or dict; normalize
            if isinstance(loi_text, dict):
                return jsonify(loi_text)
            ev = store_evidence_binary("loi_generated", json.dumps({"address": prop.get("address"), "preview": (str(loi_text)[:500])}).encode("utf-8"), {"address": prop.get("address")})
            return jsonify({"ok": True, "loi": str(loi_text), "evidence": ev})
        # fallback: use provider shim with a LOI instruction
        prop = payload.get("property") or payload
        prompt = payload.get("prompt") or f"Generate a professional, assignable Letter of Intent (LOI) between 2,000 and 2,500 words from the following property data: {json.dumps(prop, ensure_ascii=False)}"
        # use message format
        messages = [{"role": "system", "content": _LLM_PROMPT if _LLM_PROMPT else "You are an expert underwriter and LOI writer."},
                    {"role": "user", "content": prompt}]
        res = _shim_provider_from_factory(messages=messages, max_tokens=int(payload.get("max_tokens", 4000)))
        if not res.get("ok"):
            # deterministic fallback
            if _deterministic_fallback:
                txt = _deterministic_fallback(prompt, min_words=int(payload.get("min_words", 2000)))
                ev = store_evidence_binary("loi_fallback", txt.encode("utf-8"), {"fallback": True})
                return jsonify({"ok": True, "provider": "DETERMINISTIC_FALLBACK", "loi": txt, "evidence": ev})
            return jsonify({"ok": False, "error": "provider_failed", "detail": res}), 502
        # store evidence for generated LOI (preview only)
        preview = (res.get("response") or "")[:1000]
        ev = store_evidence_binary("loi_generated", preview.encode("utf-8"), {"provider": res.get("provider")})
        return jsonify({"ok": True, "provider": res.get("provider"), "loi": res.get("response"), "evidence": ev})
    except Exception:
        logger.exception("LOI generation exception")
        return jsonify({"ok": False, "error": "loi_exception", "tb": traceback.format_exc()}), 500

@app.route("/enqueue/analyze", methods=["POST"])
def api_enqueue_analyze():
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    payload = get_payload()
    mc_runs = int(payload.get("mc_runs", 2000))
    job_id = enqueue_job("analyze", payload, run_monte_carlo=True, mc_runs=mc_runs)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/enqueue/verify", methods=["POST"])
def api_enqueue_verify():
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    payload = get_payload()
    job_id = enqueue_job("verify", payload, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/enqueue/simulate", methods=["POST"])
def api_enqueue_simulate():
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    payload = get_payload()
    job_id = enqueue_job("simulate", payload, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/job/<job_id>", methods=["GET"])
def api_get_job(job_id: str):
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    meta = JOB_STORE.get(job_id)
    if not meta:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    return jsonify({"ok": True, "job": meta})

@app.route("/jobs", methods=["GET"])
def api_list_jobs():
    try:
        require_api_key()
    except Exception:
        return Response("Unauthorized", status=401)
    status_filter = request.args.get("status")
    jobs = [{ "id": k, "status": v.get("status"), "type": v.get("type"), "created": v.get("created")} for k, v in JOB_STORE.items()]
    if status_filter:
        jobs = [j for j in jobs if j["status"] == status_filter]
    return jsonify({"ok": True, "jobs": jobs})

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "BrittonMethod-auto", "ok": True, "time": datetime.utcnow().isoformat() + "Z", "primary_provider": MODEL_PROVIDER_PRIMARY})

# -------------------------
# Graceful shutdown
# -------------------------
def _graceful_shutdown(signum=None, frame=None):
    logger.info("Shutdown signal received: %s", signum)
    _SHUTDOWN_EVENT.set()
    # wait briefly for worker to finish
    time.sleep(0.5)
    logger.info("Shutdown complete.")

for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, _graceful_shutdown)
    except Exception:
        logger.debug("Could not set signal handler for %s", sig)

atexit.register(lambda: _SHUTDOWN_EVENT.set())

# -------------------------
# Run (debug / local)
# -------------------------
if __name__ == "__main__":
    # When running locally for quick tests, this uses Flask's threaded server.
    # For production, run with Gunicorn: gunicorn -c gunicorn_conf.py main:app
    logger.info("Starting BrittonMethod-auto (Flask debug server) on port %s", PORT)
    logger.info("Primary provider: %s; app_core=%s; provider_factory=%s", MODEL_PROVIDER_PRIMARY, APP_CORE_AVAILABLE, bool(PROVIDER_FACTORY))
    app.run(host="0.0.0.0", port=PORT, threaded=True)

