#!/usr/bin/env python3
"""
main.py — Production-ready Flask entry for BrittonMethod-auto

Preserves original application logic and provider/fallback chain,
but hardens runtime behavior for production (Render/Gunicorn), fixes
shutdown, prompt-safety, DB guards, logging, and removes syntax errors.
"""

from __future__ import annotations

import os
import json
import uuid
import time
import traceback
import hashlib
import logging
import threading
import queue
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv

# Optional: requests for external API wrappers (if installed)
try:
    import requests  # type: ignore
except Exception:
    requests = None

# --- Optional libs (SQLAlchemy, openai, numpy) ---
SQLALCHEMY_AVAILABLE = False
try:
    from sqlalchemy import create_engine  # type: ignore
    from sqlalchemy.orm import sessionmaker  # type: ignore
    # models.py optional
    from models import Base, Job  # type: ignore
    SQLALCHEMY_AVAILABLE = True
except Exception:
    SQLALCHEMY_AVAILABLE = False

# Attempt to import user-supplied app_core from the repo (preferred)
APP_CORE_AVAILABLE = False
try:
    import app_core as ac  # type: ignore
    # Bind commonly used helpers from app_core (preferred names)
    underwriter_deterministic = getattr(ac, "underwriter_deterministic", None) or (
        getattr(ac, "underwriter", None) and getattr(ac.underwriter, "deterministic", None)
    )
    monte_carlo_simulation = getattr(ac, "monte_carlo_simulation", None) or (
        getattr(ac, "underwriter", None) and getattr(ac.underwriter, "monte_carlo", None)
    )
    simulate_refund_waterfall = getattr(ac, "simulate_refund_waterfall", None) or (
        getattr(ac, "Underwriter", None) and getattr(ac.Underwriter, "simulate_refund_waterfall", None)
    )
    verify_listing_pipeline = getattr(ac, "verify_listing_pipeline", None) or (
        getattr(ac, "verifier", None) and getattr(ac.verifier, "verify", None)
    )
    store_evidence_binary = getattr(ac, "store_evidence_binary", None) or (
        getattr(ac, "evidence_store", None) and getattr(ac.evidence_store, "store_binary", None)
    )
    generate_long_loi_text = getattr(ac, "generate_long_loi_text", None) or getattr(
        ac, "generate_long_loi_text_wrapper", None
    )
    # Provider chain wrapper: prefer provider_generate_text_with_fallback, fallback to get_openai_response if present
    provider_chain_fn = getattr(ac, "provider_generate_text_with_fallback", None)
    legacy_get_openai_response = getattr(ac, "get_openai_response", None)
    # Deterministic fallback direct access
    deterministic_fallback = getattr(ac, "deterministic_fallback", None) or getattr(ac, "_deterministic_fallback_text", None)

    # Diagnostics / prompt loader
    LLM_PROMPT = getattr(ac, "LLM_PROMPT", None)
    APP_CORE_AVAILABLE = True
except Exception:
    APP_CORE_AVAILABLE = False
    underwriter_deterministic = None
    monte_carlo_simulation = None
    simulate_refund_waterfall = None
    verify_listing_pipeline = None
    store_evidence_binary = None
    generate_long_loi_text = None
    provider_chain_fn = None
    legacy_get_openai_response = None
    deterministic_fallback = None
    LLM_PROMPT = None

# If app_core provides the provider chain, create a small shim named get_openai_response that main expects.
def _shim_provider_wrapper(prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
    """
    Unified response format:
      {"ok": True, "response": "<text>", "provider": "<provider name>", "raw": <raw>}
    This wrapper calls provider_generate_text_with_fallback if available; otherwise calls legacy_get_openai_response if present;
    otherwise returns deterministic fallback text.
    """
    try:
        # Primary: provider chain (DeepSeek/Groq/OpenAI/Gemini/Local/Deterministic)
        if provider_chain_fn:
            out = provider_chain_fn(prompt, max_tokens=max_tokens)
            if isinstance(out, dict) and out.get("ok"):
                return {"ok": True, "response": out.get("response"), "provider": out.get("provider") or "UNKNOWN", "raw": out.get("raw")}
            if isinstance(out, str):
                return {"ok": True, "response": out, "provider": "PROVIDER_CHAIN_TEXT"}
        # Secondary: legacy direct helper (older app_core might export this)
        if legacy_get_openai_response:
            res = legacy_get_openai_response(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
            if isinstance(res, dict) and res.get("ok"):
                return {"ok": True, "response": res.get("response"), "provider": "legacy_get_openai_response", "raw": res}
            if isinstance(res, str):
                return {"ok": True, "response": res, "provider": "legacy_get_openai_response"}
        # Final: deterministic fallback (always available)
        if deterministic_fallback:
            text = deterministic_fallback(prompt, min_words=200)
            return {"ok": True, "response": text, "provider": "DETERMINISTIC_FALLBACK"}
        # Last resort: basic echo
        return {"ok": True, "response": f"(fallback echo) {prompt}", "provider": "ECHO_FALLBACK"}
    except Exception as e:
        return {"ok": False, "error": str(e), "provider": "EXCEPTION"}


# Expose get_openai_response for the rest of main to call
get_openai_response = _shim_provider_wrapper

# --- Load .env ---
load_dotenv()

# --- Environment variables & mappings ---
API_KEY = os.environ.get("BRITTON_API_KEY", "")  # required header/key for endpoints if present
# Accept both ATTOM_KEY and ATTOM_API_KEY
ATTOM_API_KEY = os.environ.get("ATTOM_KEY", "") or os.environ.get("ATTOM_API_KEY", "")
# Twilio mapping
TWILIO_SID = os.environ.get("TWILIO_API_SID", "") or os.environ.get("TWILIO_SID", "")
TWILIO_AUTH = os.environ.get("TWILIO_API_SECRET", "") or os.environ.get("TWILIO_AUTH", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
# OpenAI / Deepseek / Groq / Gemini / Tavily / others
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = os.environ.get("GROQ_API_URL", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "") or os.environ.get("GEMINI_TOKEN", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
# Evidence / S3
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "us-east-1"))
# Other config
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///britton.db")
REDIS_URL = os.environ.get("REDIS_URL", "")
HUMAN_IN_LOOP_VALUE = float(os.environ.get("HUMAN_IN_LOOP_VALUE", "50000000"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "10"))

# ensure evidence dir exists
os.makedirs(EVIDENCE_DIR, exist_ok=True)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("britton")

# --- Personas (optional) ---
try:
    import britton_personas as personas_mod  # type: ignore

    BRITTON_PERSONAS = getattr(personas_mod, "BRITTON_PERSONAS", None)
    assign_persona_stack = getattr(personas_mod, "assign_persona_stack", None)
except Exception:
    BRITTON_PERSONAS = None
    assign_persona_stack = None

# --- DB (optional) ---
SessionLocal = None
if SQLALCHEMY_AVAILABLE:
    try:
        engine = create_engine(
            DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
        )
        SessionLocal = sessionmaker(bind=engine)
        try:
            # Base may not exist or models may be missing; guard it
            if "Base" in globals() and hasattr(Base, "metadata"):
                Base.metadata.create_all(bind=engine)
        except Exception:
            logger.exception("Failed creating DB metadata (models may be missing or broken).")
    except Exception:
        logger.exception("SQLAlchemy engine setup failed.")
        SessionLocal = None

# --- Job queue & in-memory store ---
JOB_QUEUE = queue.Queue()
JOB_STORE: Dict[str, Dict[str, Any]] = {}
# Shutdown event for graceful termination
_SHUTDOWN_EVENT = threading.Event()

# --- Helper defs ---
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def mask_key(val: Optional[str]) -> str:
    if not val:
        return "MISSING"
    s = str(val)
    if len(s) <= 8:
        return s[:2] + "..." + s[-2:]
    return s[:4] + "..." + s[-4:]


def require_api_key():
    if not API_KEY:
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if key == API_KEY:
        return True
    abort(401, description="Missing or invalid API key.")


def get_payload() -> Dict[str, Any]:
    p = request.get_json(silent=True)
    if p is None:
        raw = request.get_data(as_text=True) or ""
        raw = raw.strip()
        if raw == "":
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                return {"prompt": parsed}
            if isinstance(parsed, dict):
                return parsed
            return {"_raw": parsed}
        except Exception:
            return {"prompt": raw}
    if isinstance(p, str):
        return {"prompt": p}
    if isinstance(p, dict):
        return p
    return {"_raw": str(p)}


def enqueue_job(job_type: str, payload: Dict[str, Any], run_monte_carlo: bool = True, mc_runs: int = 2000) -> str:
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat() + "Z", "status": "queued", "payload": payload}
    JOB_QUEUE.put({"job_id": job_id, "type": job_type, "payload": payload, "monte_carlo": run_monte_carlo, "mc_runs": mc_runs})
    if SessionLocal:
        try:
            db = SessionLocal()
            job_row = Job(id=job_id, payload=payload, status="queued")
            db.add(job_row)
            db.commit()
            db.close()
        except Exception:
            logger.exception("DB persist job failed")
    return job_id


def worker_loop():
    logger.info("Background worker starting (in-process fallback).")
    # job variable defined here so exception handling below can reference it safely
    job = None
    while not _SHUTDOWN_EVENT.is_set():
        try:
            job = JOB_QUEUE.get()
            # sentinel for shutdown
            if job is None:
                logger.info("Worker received shutdown sentinel.")
                break

            job_id = job.get("job_id")
            JOB_STORE[job_id]["status"] = "running"
            t0 = time.time()

            if job["type"] == "analyze":
                prop = job["payload"]
                try:
                    det = (underwriter_deterministic(prop) if underwriter_deterministic else None)
                except Exception:
                    logger.exception("deterministic underwriter failed")
                    det = None
                try:
                    mc = (monte_carlo_simulation(prop, runs=job.get("mc_runs", 2000)) if monte_carlo_simulation else None)
                except Exception:
                    logger.exception("monte carlo failed")
                    mc = None
                try:
                    raw = json.dumps({"property": prop, "det": det, "mc": mc}).encode("utf-8")
                    ev = (store_evidence_binary("analysis_bundle", raw, {"address": prop.get("address")}) if store_evidence_binary else None)
                except Exception:
                    ev = None
                result = {"ok": True, "deal": det, "monte_carlo": mc, "evidence": ev}
                try:
                    if det and (det.get("britton_score", 0) >= 95 or det.get("price", 0) >= HUMAN_IN_LOOP_VALUE or det.get("confidence", 0) < CONFIDENCE_THRESHOLD):
                        JOB_STORE[job_id]["status"] = "escalated"
                        JOB_STORE[job_id]["result"] = result
                        JOB_STORE[job_id]["escalation"] = {"reason": "human_in_loop_threshold", "created": datetime.utcnow().isoformat() + "Z"}
                    else:
                        JOB_STORE[job_id]["status"] = "done"
                        JOB_STORE[job_id]["result"] = result
                except Exception:
                    JOB_STORE[job_id]["status"] = "done"
                    JOB_STORE[job_id]["result"] = result

            elif job["type"] == "verify":
                listing = job["payload"]
                try:
                    manifest = verify_listing_pipeline(listing, attempts=MAX_VERIFICATION_ATTEMPTS, require_checks=5) if verify_listing_pipeline else {"ok": False, "error": "verify pipeline not available"}
                except Exception:
                    manifest = {"ok": False, "error": "verify_listing_pipeline not available or failed"}
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = manifest

            elif job["type"] == "simulate":
                payload = job["payload"]
                try:
                    sim = simulate_refund_waterfall(float(payload.get("price", 0)), float(payload.get("existing_debt", 0)), float(payload.get("investor_equity_pct", 0.25))) if simulate_refund_waterfall else {"ok": False, "error": "simulate not available"}
                except Exception:
                    sim = {"ok": False, "error": "simulate_refund_waterfall failed"}
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = {"ok": True, "simulation": sim}

            else:
                JOB_STORE[job_id]["status"] = "error"
                JOB_STORE[job_id]["result"] = {"ok": False, "error": "unknown job type"}

            t1 = time.time()
            JOB_STORE[job_id]["duration_seconds"] = t1 - t0

            if SessionLocal:
                try:
                    db = SessionLocal()
                    j = db.query(Job).filter(Job.id == job_id).first()
                    if j:
                        j.status = JOB_STORE[job_id]["status"]
                        j.result = JOB_STORE[job_id].get("result")
                        db.commit()
                    db.close()
                except Exception:
                    logger.exception("DB update job failed")

        except Exception as e:
            job_id_local = None
            try:
                job_id_local = job.get("job_id") if isinstance(job, dict) else "unknown"
            except Exception:
                job_id_local = "unknown"
            JOB_STORE.setdefault(job_id_local, {})["status"] = "error"
            JOB_STORE[job_id_local]["result"] = {"ok": False, "error": str(e), "tb": traceback.format_exc()}
            logger.exception("Worker loop exception")
        finally:
            try:
                JOB_QUEUE.task_done()
            except Exception:
                pass


_worker_thread = threading.Thread(target=worker_loop, daemon=True)
_worker_thread.start()

# --- Flask app ---
app = Flask(__name__)

@app.route("/")
def index():
    return (
        "<h1>Britton Method API</h1>"
        "<p>Endpoints: /health, /analyze, /verify, /generate-loi, /waterfall/simulate, "
        "/webhook/email, /persona-stack, /diagnostics/run, /diagnostics/env, /job-status/&lt;id&gt;, /nlp</p>"
    )

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat() + "Z", "jobs_total": len(JOB_STORE)})

@app.route("/analyze", methods=["POST"])
def analyze_route():
    if API_KEY:
        require_api_key()
    payload = get_payload()
    prop = payload.get("property") if isinstance(payload, dict) else None
    if not prop:
        prop = payload
    if not prop or (isinstance(prop, dict) and not prop):
        return jsonify({"ok": False, "error": "missing property"}), 400
    job_id = enqueue_job("analyze", prop, run_monte_carlo=bool(payload.get("run_monte_carlo", True)), mc_runs=int(payload.get("mc_runs", 2000)))
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/verify", methods=["POST"])
def verify_route():
    if API_KEY:
        require_api_key()
    payload = get_payload()
    listing = payload.get("listing") if isinstance(payload, dict) else None
    if not listing:
        listing = payload
    if not listing or (isinstance(listing, dict) and not listing):
        return jsonify({"ok": False, "error": "missing listing"}), 400
    job_id = enqueue_job("verify", listing, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/generate-loi", methods=["POST"])
def loi_route():
    if API_KEY:
        require_api_key()
    payload = get_payload()
    prop = payload.get("property") if isinstance(payload, dict) else payload
    if not prop:
        return jsonify({"ok": False, "error": "missing property"}), 400
    for k in ("britton_score", "confidence", "internal_notes", "evidence_manifest"):
        if isinstance(prop, dict):
            prop.pop(k, None)
    try:
        loi = generate_long_loi_text(prop) if generate_long_loi_text else "LOI generator not available"
    except Exception as e:
        return jsonify({"ok": False, "error": f"LOI generation failed: {str(e)}"}), 500
    ev = None
    try:
        ev = store_evidence_binary("loi_generated", loi.encode("utf-8"), {"address": prop.get("address")}) if store_evidence_binary else None
    except Exception:
        ev = None
    return jsonify({"ok": True, "loi": loi, "evidence": ev}), 200

@app.route("/waterfall/simulate", methods=["POST"])
def waterfall_route():
    if API_KEY:
        require_api_key()
    payload = get_payload()
    price = payload.get("price")
    existing_debt = payload.get("existing_debt", 0)
    g = payload.get("investor_equity_pct", 0.25)
    if price is None:
        return jsonify({"ok": False, "error": "missing price"}), 400
    job_id = enqueue_job("simulate", {"price": price, "existing_debt": existing_debt, "investor_equity_pct": g}, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/webhook/email", methods=["POST"])
def webhook_email():
    payload = get_payload()
    parsed = payload.get("parsed") if isinstance(payload, dict) else None
    if not parsed:
        return jsonify({"ok": False, "error": "no parsed property"}), 400
    verify_job_id = enqueue_job("verify", parsed, run_monte_carlo=False)
    analysis_job_id = enqueue_job("analyze", parsed, run_monte_carlo=True, mc_runs=2000)
    return jsonify({"ok": True, "verify_job_id": verify_job_id, "analysis_job_id": analysis_job_id}), 202

@app.route("/persona-stack", methods=["POST"])
def persona_route():
    if API_KEY:
        require_api_key()
    payload = get_payload()
    max_personas = int(payload.get("max_personas", 8)) if isinstance(payload, dict) else 8
    if assign_persona_stack:
        try:
            stack = assign_persona_stack(payload, max_personas=max_personas)
            return jsonify({"ok": True, "stack": stack})
        except Exception:
            pass
    if BRITTON_PERSONAS:
        out = [{"id": k, "persona": BRITTON_PERSONAS[k]} for k in sorted(BRITTON_PERSONAS.keys())[:max_personas]]
        return jsonify({"ok": True, "stack": out})
    return jsonify({"ok": False, "error": "personas module not available"}), 500

@app.route("/diagnostics/run", methods=["POST", "GET"])
def diagnostics_run():
    if API_KEY:
        require_api_key()
    results = {"time": datetime.utcnow().isoformat() + "Z", "checks": []}
    try:
        sample = b"britton-diagnostics"
        ev = store_evidence_binary("diag_sample", sample, {"note": "diagnostic"}) if store_evidence_binary else None
        if ev:
            results["checks"].append({"name": "evidence_store", "ok": True, "item": {"id": ev.get("id"), "path": ev.get("local_path")}})
        else:
            results["checks"].append({"name": "evidence_store", "ok": False, "error": "store_evidence_binary not available"})
    except Exception as e:
        results["checks"].append({"name": "evidence_store", "ok": False, "error": str(e)})
    results["checks"].append({"name": "llm_prompt_loaded", "ok": bool(LLM_PROMPT)})
    results["checks"].append({"name": "personas", "ok": bool(BRITTON_PERSONAS), "count": len(BRITTON_PERSONAS) if BRITTON_PERSONAS else 0})
    results["checks"].append({"name": "provider_chain_present", "ok": bool(provider_chain_fn)})
    results["checks"].append({"name": "deterministic_fallback_present", "ok": bool(deterministic_fallback)})
    results["checks"].append({"name": "numpy_available", "ok": ('numpy' in globals() and globals().get('numpy') is not None)})
    results["checks"].append({"name": "attom_available", "ok": bool(ATTOM_API_KEY)})
    results["checks"].append({"name": "twilio_available", "ok": bool(TWILIO_SID and TWILIO_AUTH)})
    results["checks"].append({"name": "gemini_available", "ok": bool(GEMINI_KEY)})
    results["checks"].append({"name": "tavily_available", "ok": bool(TAVILY_API_KEY)})
    results["jobs_total"] = len(JOB_STORE)
    return jsonify(results), 200

@app.route("/diagnostics/env", methods=["GET"])
def diagnostics_env():
    if API_KEY:
        require_api_key()
    env_report = {
        "OPENAI_API_KEY": mask_key(OPENAI_KEY),
        "DEEPSEEK_API_KEY": mask_key(DEEPSEEK_API_KEY),
        "GROQ_API_KEY": mask_key(GROQ_API_KEY),
        "BRITTON_API_KEY": mask_key(API_KEY),
        "ATTOM_API_KEY": mask_key(ATTOM_API_KEY),
        "GEMINI_KEY": mask_key(GEMINI_KEY),
        "TAVILY_API_KEY": mask_key(TAVILY_API_KEY),
        "REDIS_URL": mask_key(REDIS_URL),
        "DATABASE_URL": mask_key(DATABASE_URL),
        "USE_S3": os.environ.get("USE_S3", "false"),
        "S3_BUCKET": os.environ.get("S3_BUCKET", "")
    }
    return jsonify({"ok": True, "env": env_report}), 200

@app.route("/job-status/<job_id>", methods=["GET"])
def job_status(job_id):
    if API_KEY:
        require_api_key()
    job = JOB_STORE.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify(job), 200

@app.route("/shutdown", methods=["POST"])
def shutdown():
    if API_KEY:
        require_api_key()
    # signal worker to stop and enqueue sentinel
    try:
        _SHUTDOWN_EVENT.set()
        JOB_QUEUE.put(None)
        # attempt to join thread (non-blocking, short timeout)
        if _worker_thread.is_alive():
            _worker_thread.join(timeout=5)
    except Exception:
        logger.exception("shutdown encountered an error")
    return jsonify({"ok": True, "message": "shutdown queued"}), 200

# --- Natural-language (Chat-like) endpoint ---
@app.route("/nlp", methods=["POST", "GET"])
def nlp_route():
    if API_KEY:
        require_api_key()

    payload = get_payload()
    if request.method == "GET" and not payload:
        prompt = request.args.get("prompt") or request.args.get("q") or ""
        payload = {"prompt": prompt} if prompt else {}
    if not isinstance(payload, dict):
        payload = {"prompt": str(payload)}

    prompt = (payload.get("prompt") or payload.get("query") or "") if isinstance(payload, dict) else ""
    if not prompt:
        return jsonify({"ok": False, "error": "missing prompt"}), 400

    # Clip prompt length for logging & provider safety (avoid massive memory usage)
    max_prompt_log = 10000
    prompt_for_log = prompt if len(prompt) <= max_prompt_log else (prompt[:max_prompt_log] + "...[truncated]")
    model = payload.get("model", os.environ.get("MODEL_PROVIDER_PRIMARY", "gpt-4o-mini"))
    max_tokens = int(payload.get("max_tokens", 512)) if payload.get("max_tokens") is not None else 512
    temperature = float(payload.get("temperature", 0.0)) if payload.get("temperature") is not None else 0.0

    logger.info("NLP request model=%s prompt_len=%d", model, min(len(prompt), max_prompt_log))

    try:
        # Call the unified provider shim (this will try provider chain, legacy helper, then deterministic)
        out = get_openai_response(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
    except Exception as e:
        logger.exception("Provider call exception")
        return jsonify({"ok": False, "error": f"Provider call failed: {str(e)}"}), 500

    # Normalize response shapes
    if isinstance(out, dict) and out.get("ok"):
        resp_text = out.get("response")
        provider = out.get("provider", "unknown")
        return jsonify({"ok": True, "provider": provider, "response": resp_text}), 200
    # legacy string response
    if isinstance(out, str):
        return jsonify({"ok": True, "provider": "legacy-string", "response": out}), 200
    # error case
    return jsonify({"ok": False, "error": "provider returned failure", "details": out}), 500

# --- Run server ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting BrittonMethod API on port %s", port)
    logger.info(
        "Providers: provider_chain_fn=%s legacy_get_openai_response=%s deterministic_fallback=%s",
        bool(provider_chain_fn),
        bool(legacy_get_openai_response),
        bool(deterministic_fallback),
    )
    app.run(host="0.0.0.0", port=port, threaded=True)
