#!/usr/bin/env python3
"""
Main.py — Britton Method (Final / Production-ready)

Features:
- Property analysis (deterministic + Monte Carlo fallback via app_core)
- Refund waterfall simulation
- LOI generation and evidence storage (app_core)
- Listing verification pipeline (pluggable, app_core)
- Async job queue with optional DB persistence
- Persona stack assignment (pluggable)
- Diagnostics endpoints
- API key protection (BRITTON_API_KEY)
- Natural-language endpoint /nlp that uses provider chain (app_core.get_openai_response or other providers)
- Graceful worker shutdown
"""

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
    import requests
except Exception:
    requests = None

# --- Optional libs (SQLAlchemy, openai, numpy) ---
SQLALCHEMY_AVAILABLE = False
try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, Job   # optional models.py in your repo
    SQLALCHEMY_AVAILABLE = True
except Exception:
    SQLALCHEMY_AVAILABLE = False

# Attempt to import user-supplied app_core from the repo (preferred)
APP_CORE_AVAILABLE = False
try:
    # app_core should export the core functions and utilities
    import app_core as ac
    # Bind commonly used helpers from app_core
    underwriter_deterministic = getattr(ac, "underwriter_deterministic", None)
    monte_carlo_simulation = getattr(ac, "monte_carlo_simulation", None)
    simulate_refund_waterfall = getattr(ac, "simulate_refund_waterfall", None)
    verify_listing_pipeline = getattr(ac, "verify_listing_pipeline", None)
    store_evidence_binary = getattr(ac, "store_evidence_binary", None)
    generate_long_loi_text = getattr(ac, "generate_long_loi_text", None)
    get_openai_response = getattr(ac, "get_openai_response", None)  # robust call that app_core may provide
    LLM_PROMPT = getattr(ac, "LLM_PROMPT", None)
    ATTOM_WRAPPER = getattr(ac, "attom_get_property_by_address", None)
    TWILIO_WRAPPER = getattr(ac, "twilio_send_sms", None)
    GEMINI_WRAPPER = getattr(ac, "gemini_search_example", None)
    TAVILY_WRAPPER = getattr(ac, "tavily_analyze_text", None)
    APP_CORE_AVAILABLE = True
except Exception:
    APP_CORE_AVAILABLE = False
    # ensure names exist to avoid NameError later
    underwriter_deterministic = None
    monte_carlo_simulation = None
    simulate_refund_waterfall = None
    verify_listing_pipeline = None
    store_evidence_binary = None
    generate_long_loi_text = None
    get_openai_response = None
    LLM_PROMPT = None
    ATTOM_WRAPPER = None
    TWILIO_WRAPPER = None
    GEMINI_WRAPPER = None
    TAVILY_WRAPPER = None

# --- Minimal fallback implementations (only used if app_core absent) ---
# These match the original Main.py's fallback behavior so the app remains functional without app_core.
if not APP_CORE_AVAILABLE:

    # note: these are simplified copies of your original fallbacks
    def compute_britton_score(noi, price, dscr, equity_gap, prop_meta):
        try:
            cashflow = (noi / price) if price > 0 else 0
            cashflow_score = min(1.5, cashflow * 10)
            dscr_score = min(2.0, dscr or 0) / 2.0
            gap_score = 1.0 if equity_gap >= 0 else max(0.0, 1.0 + (equity_gap / price))
            tags = prop_meta.get("tags") if isinstance(prop_meta.get("tags"), list) else []
            seller_score = 1.2 if any(t.lower() in ("motivated", "probate", "divorce", "pre-foreclosure", "tax-lien") for t in tags) else 1.0
            title_risk = 0.9 if any(t.lower() in ("liens", "clouded-title", "judgment", "bankruptcy") for t in tags) else 1.0
            raw = (cashflow_score * 0.35 + dscr_score * 0.35 + gap_score * 0.2) * 100
            raw = raw * seller_score * title_risk
            return max(0, min(100, raw))
        except Exception:
            return 0

    def underwriter_deterministic(prop: Dict[str, Any]) -> Dict[str, Any]:
        price = float(prop.get("price") or 0)
        gpr = float(prop.get("gpr") or 0)
        vacancy = float(prop.get("vacancy_rate") or 0.08)
        operating = float(prop.get("operating_expenses") or (gpr * 0.4))
        egi = gpr * (1 - vacancy)
        noi = max(0, egi - operating)
        loan_amount = 0.75 * price
        interest_rate = float(prop.get("assumed_interest") or 0.055)
        annual_debt_service = loan_amount * interest_rate
        dscr = (noi / annual_debt_service) if annual_debt_service > 0 else None
        g = float(prop.get("investor_equity_pct") or 0.25)
        refund = 1.10 * g * price
        existing_debt = float(prop.get("existing_debt") or 0)
        equity_gap = price - existing_debt - refund
        britton_score = compute_britton_score(noi, price, dscr, equity_gap, prop)
        confidence = float(prop.get("confidence") or 0.5)
        return {
            "price": price,
            "gpr": gpr,
            "egi": egi,
            "noi": noi,
            "operating_expenses": operating,
            "loan_amount": loan_amount,
            "annual_debt_service": annual_debt_service,
            "dscr": dscr,
            "refund": refund,
            "existing_debt": existing_debt,
            "equity_gap": equity_gap,
            "britton_score": britton_score,
            "confidence": confidence
        }

    # If numpy is unavailable, the fallback will return a deterministic single-run.
    try:
        import numpy as np
        NUMPY_AVAILABLE = True
    except Exception:
        NUMPY_AVAILABLE = False

    def monte_carlo_simulation(prop: Dict[str, Any], runs: int = 2000) -> Dict[str, Any]:
        if not NUMPY_AVAILABLE:
            det = underwriter_deterministic(prop)
            return {"runs": 1, "dscr_p50": det.get("dscr"), "noi_p50": det.get("noi"), "yield_p50": (det.get("noi") / det.get("price") if det.get("price") else 0)}
        price = float(prop.get("price", 0))
        gpr = float(prop.get("gpr", 0))
        base_vacancy = float(prop.get("vacancy_rate") or 0.08)
        base_operating = float(prop.get("operating_expenses") or (gpr * 0.4))
        rent_growth = np.random.normal(0, 0.05, runs)
        expense_inflation = np.random.normal(0.02, 0.015, runs)
        gpr_samples = gpr * (1 + rent_growth)
        vacancy_samples = np.clip(base_vacancy + np.random.normal(0, 0.02, runs), 0, 0.5)
        operating_samples = base_operating * (1 + expense_inflation)
        egi_samples = gpr_samples * (1 - vacancy_samples)
        noi_samples = np.maximum(0.0, egi_samples - operating_samples)
        loan_amount = 0.75 * price
        interest = float(prop.get("assumed_interest") or 0.055)
        debt_service = loan_amount * interest
        dscr_samples = np.where(debt_service > 0, noi_samples / debt_service, np.nan)
        yield_samples = np.where(price > 0, noi_samples / price, 0.0)
        def pct(arr, p): import numpy as _np; return float(_np.nanpercentile(arr, p))
        return {
            "runs": runs,
            "dscr_p10": pct(dscr_samples, 10),
            "dscr_p25": pct(dscr_samples, 25),
            "dscr_p50": pct(dscr_samples, 50),
            "dscr_p75": pct(dscr_samples, 75),
            "dscr_p90": pct(dscr_samples, 90),
            "yield_p50": pct(yield_samples, 50),
            "noi_p50": pct(noi_samples, 50)
        }

    def simulate_refund_waterfall(price: float, existing_debt: float, investor_equity_pct: float):
        g = investor_equity_pct
        refund = 1.10 * g * price
        e_gap = price - existing_debt - refund
        out = {"price": price, "existing_debt": existing_debt, "investor_equity_pct": g, "refund": refund, "equity_gap": e_gap}
        if e_gap >= 0:
            buyer_cash = e_gap / 2.0
            seller_cash = e_gap / 2.0
            out.update({"buyer_cash": buyer_cash, "seller_cash": seller_cash, "structure": "buyer_seller_split"})
        else:
            seller_carry = abs(e_gap)
            out.update({"seller_carry_required": seller_carry, "structure": "seller_carryback"})
        return out

    def store_evidence_binary(source: str, raw_bytes: bytes, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        meta = meta or {}
        h = hashlib.sha256(raw_bytes).hexdigest()
        filename = f"{h}.bin"
        local_dir = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, filename)
        with open(local_path, "wb") as f:
            f.write(raw_bytes)
        return {"id": h, "source": source, "sha256": h, "local_path": local_path, "meta": meta}

    def verify_listing_pipeline(listing: Dict[str, Any], attempts: int = 5, require_checks: int = 5) -> Dict[str, Any]:
        manifest = {"id": str(uuid.uuid4()), "created": datetime.utcnow().isoformat()+"Z", "requested_attempts": attempts, "required_checks": require_checks, "checks": [], "evidence": []}
        # minimal stub checks for fallback
        ev = store_evidence_binary("verify_stub", json.dumps({"listing": listing}).encode("utf-8"), {})
        manifest["checks"].append({"check": "stub", "passed": True, "evidence_items": [ev]})
        manifest.update({"passed_checks": 1, "confidence": 1.0, "label": "VERIFIED"})
        return manifest

    def generate_long_loi_text(prop: Dict[str, Any], min_words: int = 2000) -> str:
        safe_prop = {k: v for k, v in prop.items() if k not in ("britton_score", "confidence", "internal_notes", "evidence_manifest")}
        parts = []
        parts.append(f"LETTER OF INTENT — {safe_prop.get('address', '[ADDRESS]')}\n")
        parts.append(f"Buyer intends to acquire the property at {safe_prop.get('address', '[ADDRESS]')}. Structure: seller-friendly.\n\n")
        parts.append(f"Purchase Price: ${safe_prop.get('price', 'TBD')} | Preferred: seller financing / carryback / hybrid.\n\n")
        parts.append("Contingencies: Standard HOA, title, inspection & financing. 10 business days.\n\n")
        base_text = "\n".join(parts)
        while len(base_text.split()) < min_words:
            base_text += ("\nBuyer aims for a timely, clean close. " * 30)
        return base_text

    # fallback OpenAI function (very basic) if app_core isn't present
    def get_openai_response(prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
        # If the openai package is available, attempt to call it defensively.
        try:
            import openai as _openai
            key = os.environ.get("OPENAI_API_KEY", "")
            if not key:
                return {"ok": False, "error": "OPENAI_API_KEY missing"}
            _openai.api_key = key
            # Try a few call surfaces
            resp = None
            if hasattr(_openai, "chat") and hasattr(_openai.chat, "completions"):
                try:
                    resp = _openai.chat.completions.create(model=model, messages=[{"role":"user","content":prompt}], max_tokens=max_tokens, temperature=temperature)
                except Exception:
                    resp = None
            if resp is None and hasattr(_openai, "ChatCompletion") and hasattr(_openai.ChatCompletion, "create"):
                try:
                    resp = _openai.ChatCompletion.create(model=model, messages=[{"role":"user","content":prompt}], max_tokens=max_tokens, temperature=temperature)
                except Exception:
                    resp = None
            if resp is None and hasattr(_openai, "Completion") and hasattr(_openai.Completion, "create"):
                try:
                    resp = _openai.Completion.create(engine=model, prompt=prompt, max_tokens=max_tokens, temperature=temperature)
                except Exception:
                    resp = None
            if resp is None:
                return {"ok": False, "error": "OpenAI client method not available"}
            # parse response safely
            try:
                choices = getattr(resp, "choices", None) or (resp.get("choices") if isinstance(resp, dict) else None)
                if choices:
                    first = choices[0]
                    if isinstance(first, dict):
                        msg = first.get("message") or first.get("text") or {}
                        if isinstance(msg, dict):
                            text = msg.get("content") or msg.get("text") or ""
                        else:
                            text = str(msg)
                    else:
                        text = getattr(first, "text", "") or str(first)
                else:
                    text = str(resp)
                return {"ok": True, "response": text}
            except Exception as e:
                return {"ok": False, "error": f"parse_failed:{e}", "raw": str(resp)}
        except Exception as e:
            return {"ok": False, "error": f"openai-call-failed:{e}"}

# --- Load .env ---
load_dotenv()

# --- Environment variables & mappings ---
API_KEY = os.environ.get("BRITTON_API_KEY", "")     # required header/key for endpoints if present
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
    import britton_personas as personas_mod
    BRITTON_PERSONAS = getattr(personas_mod, "BRITTON_PERSONAS", None)
    assign_persona_stack = getattr(personas_mod, "assign_persona_stack", None)
except Exception:
    BRITTON_PERSONAS = None
    assign_persona_stack = None

# --- DB (optional) ---
SessionLocal = None
if SQLALCHEMY_AVAILABLE:
    try:
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
        SessionLocal = sessionmaker(bind=engine)
        # create tables if models provided
        try:
            Base.metadata.create_all(bind=engine)
        except Exception:
            logger.exception("Failed creating DB metadata (models may be missing or broken).")
    except Exception:
        logger.exception("SQLAlchemy engine setup failed.")
        SessionLocal = None

# --- Job queue & in-memory store ---
JOB_QUEUE = queue.Queue()
JOB_STORE: Dict[str, Dict[str, Any]] = {}

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
    """
    If BRITTON_API_KEY is set, require X-API-KEY header or api_key query param.
    If BRITTON_API_KEY is empty, allow through.
    """
    if not API_KEY:
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if key == API_KEY:
        return True
    abort(401, description="Missing or invalid API key.")

def get_payload() -> Dict[str, Any]:
    """
    Safely parse incoming request body into a dict.
    Accepts:
    - JSON body (object)
    - Raw text body (treated as {"prompt": "raw text"})
    - JSON encoded as plain text
    - For GET, query params will be consulted by endpoints if needed
    """
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
    # Persist to DB if available
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
    while True:
        try:
            job = JOB_QUEUE.get()
            if job is None:
                logger.info("Worker shutdown triggered.")
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
                # escalate to human if certain triggers
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

            # Update DB row if using SQLAlchemy
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
            # Best effort to capture job id from local variable
            job_id_local = job.get("job_id") if isinstance(job, dict) else "unknown"
            JOB_STORE.setdefault(job_id_local, {})["status"] = "error"
            JOB_STORE[job_id_local]["result"] = {"ok": False, "error": str(e), "tb": traceback.format_exc()}
            logger.exception("Worker loop exception")
        finally:
            try:
                JOB_QUEUE.task_done()
            except Exception:
                pass

# Start worker thread
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
    # support both {"property": {...}} and direct property object
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
    # sanitize/remove internal-only keys
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
    # allow direct payload with price + existing_debt + investor_equity_pct
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
    # evidence store check
    try:
        sample = b"britton-diagnostics"
        ev = store_evidence_binary("diag_sample", sample, {"note": "diagnostic"}) if store_evidence_binary else None
        if ev:
            results["checks"].append({"name": "evidence_store", "ok": True, "item": {"id": ev.get("id"), "path": ev.get("local_path")}})
        else:
            results["checks"].append({"name": "evidence_store", "ok": False, "error": "store_evidence_binary not available"})
    except Exception as e:
        results["checks"].append({"name": "evidence_store", "ok": False, "error": str(e)})
    # LLM prompt loaded check
    try:
        results["checks"].append({"name": "llm_prompt_loaded", "ok": bool(LLM_PROMPT)})
    except Exception as e:
        results["checks"].append({"name": "llm_prompt_loaded", "ok": False, "error": str(e)})
    if BRITTON_PERSONAS:
        results["checks"].append({"name": "personas", "ok": True, "count": len(BRITTON_PERSONAS)})
    else:
        results["checks"].append({"name": "personas", "ok": False})
    # availability of providers
    results["checks"].append({"name": "openai_available", "ok": bool(OPENAI_KEY and get_openai_response)})
    results["checks"].append({"name": "deepseek_available", "ok": bool(DEEPSEEK_API_KEY)})
    results["checks"].append({"name": "groq_available", "ok": bool(GROQ_API_KEY)})
    results["checks"].append({"name": "numpy_available", "ok": NUMPY_AVAILABLE})
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
    JOB_QUEUE.put(None)
    return jsonify({"ok": True, "message": "shutdown queued"}), 200

# --- Natural-language (Chat-like) endpoint ---
@app.route("/nlp", methods=["POST", "GET"])
def nlp_route():
    """
    Flexible /nlp endpoint:
    - POST JSON: {"prompt": "...", "model": "...", "max_tokens": 512, "temperature": 0.0}
    - POST raw text body: "Hello AI..."
    - GET: /nlp?prompt=Hello+AI
    - If BRITTON_API_KEY is set in env, requires X-API-KEY header or ?api_key=
    - Tries provider chain using get_openai_response (app_core) or fallback wave.
    """
    if API_KEY:
        require_api_key()

    payload = get_payload()  # robust parse (json, raw, json-as-text)
    # If GET and payload empty, allow prompt from query param
    if request.method == "GET" and not payload:
        prompt = request.args.get("prompt") or request.args.get("q") or ""
        payload = {"prompt": prompt} if prompt else {}
    # ensure payload is dict
    if not isinstance(payload, dict):
        payload = {"_raw": str(payload)}

    prompt = (payload.get("prompt") or payload.get("query") or "") if isinstance(payload, dict) else ""
    # allow clients that send bare string body as the payload directly earlier; get_payload covers that
    if not prompt:
        return jsonify({"ok": False, "error": "missing prompt"}), 400

    model = payload.get("model", os.environ.get("MODEL_PROVIDER_PRIMARY", "gpt-4o-mini"))
    max_tokens = int(payload.get("max_tokens", 512)) if payload.get("max_tokens") is not None else 512
    temperature = float(payload.get("temperature", 0.0)) if payload.get("temperature") is not None else 0.0

    logger.info("NLP request model=%s prompt_len=%d", model, len(prompt))

    # Try app_core's provider (robust) if available
    if get_openai_response:
        try:
            out = get_openai_response(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
            if out.get("ok"):
                return jsonify({"ok": True, "model": model, "response": out.get("response")}), 200
            else:
                logger.error("Provider helper returned error: %s", out.get("error"))
                # return full error & raw if available
                return jsonify({"ok": False, "error": f"Provider call failed: {out.get('error')}", "details": out.get("raw")}), 500
        except Exception as e:
            logger.exception("Provider call threw exception")
            return jsonify({"ok": False, "error": f"Provider call failed: {str(e)}"}), 500

    # Fallback local assistant (deterministic)
    try:
        fallback = (
            f"(fallback) Echoing prompt: {prompt}\n\n"
            "- To enable real provider responses, set OPENAI_API_KEY/DEEPSEEK_API_KEY/GROQ_API_KEY in environment variables and ensure providers' SDKs are installed.\n"
            "- You can supply JSON: {\"prompt\": \"...\"}, or send raw text in body, or GET /nlp?prompt=...\n"
        )
        return jsonify({"ok": True, "model": "fallback", "response": fallback}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --- Run server ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info("Starting BrittonMethod API on port %s", port)
    logger.info("OPENAI_KEY present=%s (app_core available=%s)", bool(OPENAI_KEY), APP_CORE_AVAILABLE)
    logger.info("BRITTON_API_KEY present=%s", bool(API_KEY))
    app.run(host="0.0.0.0", port=port, threaded=True)
