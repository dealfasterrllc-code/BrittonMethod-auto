#!/usr/bin/env python3
"""
Main.py — Britton Method (Final / Production-ready)

Features:
- Property analysis (deterministic + Monte Carlo fallback)
- Refund waterfall simulation
- LOI generation and evidence storage
- Listing verification pipeline (pluggable)
- Async job queue with optional DB persistence
- Persona stack assignment (pluggable)
- Diagnostics endpoints
- API key protection (BRITTON_API_KEY)
- Natural-language endpoint /nlp that uses OPENAI_API_KEY when present (updated for openai>=1.0.0)
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

OPENAI_AVAILABLE = False
try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

NUMPY_AVAILABLE = False
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    NUMPY_AVAILABLE = False

# --- Try to load app_core if you have it (keeps your custom implementations) ---
APP_CORE_AVAILABLE = False
try:
    from app_core import (
        underwriter_deterministic,
        monte_carlo_simulation,
        simulate_refund_waterfall,
        verify_listing_pipeline,
        store_evidence_binary,
        generate_long_loi_text,
        LLM_PROMPT,
    )
    APP_CORE_AVAILABLE = True
except Exception:
    APP_CORE_AVAILABLE = False

# --- Fallback implementations (only used if app_core absent) ---
if not APP_CORE_AVAILABLE:

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

    def monte_carlo_simulation(prop: Dict[str, Any], runs: int = 2000) -> Dict[str, Any]:
        # If numpy not present, run a tiny deterministic fallback
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

    LLM_PROMPT = None

# --- Load .env ---
load_dotenv()

# --- Environment variables ---
API_KEY = os.environ.get("BRITTON_API_KEY", "")     # required header/key for endpoints if present
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
ATTOM_API_KEY = os.environ.get("ATTOM_KEY", "")  # note: env variable name mapping
TWILIO_SID = os.environ.get("TWILIO_API_SID", "")  # matches Render variables you listed
TWILIO_AUTH = os.environ.get("TWILIO_API_SECRET", "")  # secret
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")  # optional from number
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")  # your earlier GEMINI_KEY var
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")  # tvly-dev...
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///britton.db")
REDIS_URL = os.environ.get("REDIS_URL", "")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
HUMAN_IN_LOOP_VALUE = float(os.environ.get("HUMAN_IN_LOOP_VALUE", "50000000"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "10"))

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
                det = underwriter_deterministic(prop)
                mc = monte_carlo_simulation(prop, runs=job.get("mc_runs", 2000)) if job.get("monte_carlo", False) else None
                try:
                    raw = json.dumps({"property": prop, "det": det, "mc": mc}).encode("utf-8")
                    ev = store_evidence_binary("analysis_bundle", raw, {"address": prop.get("address")})
                except Exception:
                    ev = None
                result = {"ok": True, "deal": det, "monte_carlo": mc, "evidence": ev}
                # escalate to human if certain triggers
                if det.get("britton_score", 0) >= 95 or det.get("price", 0) >= HUMAN_IN_LOOP_VALUE or det.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                    JOB_STORE[job_id]["status"] = "escalated"
                    JOB_STORE[job_id]["result"] = result
                    JOB_STORE[job_id]["escalation"] = {"reason": "human_in_loop_threshold", "created": datetime.utcnow().isoformat() + "Z"}
                else:
                    JOB_STORE[job_id]["status"] = "done"
                    JOB_STORE[job_id]["result"] = result

            elif job["type"] == "verify":
                listing = job["payload"]
                try:
                    manifest = verify_listing_pipeline(listing, attempts=MAX_VERIFICATION_ATTEMPTS, require_checks=5)
                except Exception:
                    manifest = {"ok": False, "error": "verify_listing_pipeline not available or failed"}
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = manifest

            elif job["type"] == "simulate":
                payload = job["payload"]
                try:
                    sim = simulate_refund_waterfall(float(payload.get("price", 0)), float(payload.get("existing_debt", 0)), float(payload.get("investor_equity_pct", 0.25)))
                except Exception:
                    sim = {"ok": False, "error": "simulate_refund_waterfall not available"}
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
        loi = generate_long_loi_text(prop)
    except Exception as e:
        return jsonify({"ok": False, "error": f"LOI generation failed: {str(e)}"}), 500
    ev = None
    try:
        ev = store_evidence_binary("loi_generated", loi.encode("utf-8"), {"address": prop.get("address")})
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

# --- External API helper wrappers (safe, optional) ---
def attom_get_property_by_address(address: str) -> Dict[str, Any]:
    """
    Minimal Attom wrapper — only used if ATTOM_API_KEY present.
    This is a minimal example; if you want expanded functionality, implement app_core.verify_listing_pipeline to use Attom properly.
    """
    if not ATTOM_API_KEY or not requests:
        return {"ok": False, "error": "attom not available or requests missing"}
    try:
        # Example Attom endpoint - adjust to the plan you have (this is illustrative)
        url = f"https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detail"
        # Attom expects query params like address or addressid depending on plan - placeholder:
        params = {"address": address}
        headers = {"Ocp-Apim-Subscription-Key": ATTOM_API_KEY}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        return {"ok": True, "status_code": r.status_code, "data": r.json() if r.content else None}
    except Exception as e:
        logger.exception("Attom call failed")
        return {"ok": False, "error": str(e)}

def twilio_send_sms(to: str, body: str) -> Dict[str, Any]:
    """
    Minimal Twilio SMS sender using REST API (if Twilio lib not installed).
    Accepts TWILIO_SID and TWILIO_AUTH in env.
    """
    if not TWILIO_SID or not TWILIO_AUTH or not requests:
        return {"ok": False, "error": "twilio config missing or requests not installed"}
    try:
        # prefer official twilio client if available
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_AUTH)
            from_number = TWILIO_FROM or os.environ.get("TWILIO_FROM", "")
            msg = client.messages.create(body=body, from_=from_number, to=to)
            return {"ok": True, "sid": msg.sid}
        except Exception:
            # fallback raw REST
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
            data = {"From": TWILIO_FROM or os.environ.get("TWILIO_FROM", ""), "To": to, "Body": body}
            r = requests.post(url, data=data, auth=(TWILIO_SID, TWILIO_AUTH), timeout=10)
            return {"ok": True, "status_code": r.status_code, "resp": r.json() if r.content else None}
    except Exception as e:
        logger.exception("Twilio send failed")
        return {"ok": False, "error": str(e)}

def gemini_search_example(q: str) -> Dict[str, Any]:
    """
    Placeholder for Gemini usage. If you want to call a Google-style API or Gemini API,
    implement this wrapper using the appropriate SDK and your GEMINI_KEY.
    """
    if not GEMINI_KEY:
        return {"ok": False, "error": "gemini key missing"}
    # We do not implement a network call here — leave as placeholder for your integration.
    return {"ok": True, "info": "gemini wrapper placeholder", "query": q}

def tavily_analyze_text(text: str) -> Dict[str, Any]:
    """
    Example Tavily (tvly) wrapper placeholder.
    """
    if not TAVILY_API_KEY or not requests:
        return {"ok": False, "error": "tavily key missing or requests not installed"}
    try:
        # Example generic POST to Tavily (replace with real endpoint)
        url = "https://api.tavily.ai/v1/analyze"
        headers = {"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"}
        r = requests.post(url, json={"text": text}, headers=headers, timeout=10)
        return {"ok": True, "status_code": r.status_code, "resp": r.json() if r.content else None}
    except Exception as e:
        logger.exception("Tavily call failed")
        return {"ok": False, "error": str(e)}

@app.route("/diagnostics/run", methods=["POST", "GET"])
def diagnostics_run():
    if API_KEY:
        require_api_key()
    results = {"time": datetime.utcnow().isoformat() + "Z", "checks": []}
    # evidence store check
    try:
        sample = b"britton-diagnostics"
        ev = store_evidence_binary("diag_sample", sample, {"note": "diagnostic"})
        results["checks"].append({"name": "evidence_store", "ok": True, "item": {"id": ev.get("id"), "path": ev.get("local_path")}})
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
    results["checks"].append({"name": "openai_available", "ok": OPENAI_AVAILABLE and bool(OPENAI_KEY)})
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

# --- OpenAI helper: robust for openai>=1.0.0 and graceful fallback ---
def get_openai_response(prompt: str, model: str, max_tokens: int = 512, temperature: float = 0.0) -> Dict[str, Any]:
    """
    Returns dict {"ok": True, "response": text} or {"ok": False, "error": ...}
    This handles both modern openai.chat.completions.create interface and
    older ChatCompletion.create if available. It is defensive.
    """
    if not OPENAI_AVAILABLE or not OPENAI_KEY:
        return {"ok": False, "error": "openai not available or OPENAI_API_KEY missing"}

    try:
        # configure API key
        try:
            # new style
            openai.api_key = OPENAI_KEY
        except Exception:
            # fallback - library might set differently; keep best effort
            pass

        # New v1.0+ API surface: openai.chat.completions.create(...)
        # But different library versions expose different attributes; handle a few variants:
        resp = None
        # Prefer the modern explicit path if present
        if hasattr(openai, "chat") and hasattr(openai.chat, "completions"):
            try:
                resp = openai.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                logger.debug("openai.chat.completions.create failed: %s", e)
                resp = None

        # Some installs still provide ChatCompletion.create — keep as backup
        if resp is None and hasattr(openai, "ChatCompletion") and hasattr(openai.ChatCompletion, "create"):
            try:
                resp = openai.ChatCompletion.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                logger.debug("openai.ChatCompletion.create failed: %s", e)
                resp = None

        # If resp is still None, try legacy Completion API as last resort (but it's deprecated)
        if resp is None and hasattr(openai, "Completion") and hasattr(openai.Completion, "create"):
            try:
                # Legacy: feed prompt straight (less ideal for chat models)
                resp = openai.Completion.create(
                    engine=model,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                logger.debug("openai.Completion.create failed: %s", e)
                resp = None

        if resp is None:
            return {"ok": False, "error": "OpenAI call failed: no available client method (check openai lib version)"}

        # Try to extract text safely from common response shapes
        try:
            # If object has .choices (OpenAI SDK returns objects with attributes)
            choices = None
            if hasattr(resp, "choices"):
                # SDK objects might support indexing -> convert to python objects where possible
                try:
                    choices = resp.choices
                except Exception:
                    try:
                        choices = resp.get("choices")
                    except Exception:
                        choices = None
            else:
                # If resp is a dict-like
                try:
                    choices = resp.get("choices")
                except Exception:
                    choices = None

            text = ""
            if choices:
                first = choices[0]
                # first may be an object with .message or .text attributes or dict
                if isinstance(first, dict):
                    # chat completion style
                    msg = first.get("message") or first.get("delta") or {}
                    if isinstance(msg, dict):
                        text = msg.get("content") or msg.get("text") or ""
                    if not text:
                        text = first.get("text") or ""
                else:
                    # object with attributes
                    try:
                        msg = getattr(first, "message", None)
                        if msg and hasattr(msg, "get"):
                            text = msg.get("content") or ""
                        elif msg and hasattr(msg, "content"):
                            text = msg.content
                        else:
                            text = getattr(first, "text", "") or ""
                    except Exception:
                        text = str(first)
            else:
                # fallback to stringifying the response
                text = str(resp)

            # final normalization
            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            if not text:
                text = str(resp)
            return {"ok": True, "response": text}
        except Exception as e:
            logger.exception("Failed parsing OpenAI response")
            return {"ok": False, "error": f"failed-parsing-response: {e}", "raw": str(resp)}

    except Exception as e:
        logger.exception("OpenAI call failed")
        return {"ok": False, "error": str(e)}

# --- Natural-language (Chat-like) endpoint ---
@app.route("/nlp", methods=["POST", "GET"])
def nlp_route():
    """
    Flexible /nlp endpoint:
    - POST JSON: {"prompt": "...", "model": "...", "max_tokens": 512, "temperature": 0.0}
    - POST raw text body: "Hello AI..."
    - GET: /nlp?prompt=Hello+AI
    - If BRITTON_API_KEY is set in env, requires X-API-KEY header or ?api_key=
    - If OPENAI_API_KEY present and openai package installed, will call OpenAI.
    - Otherwise returns a deterministic helpful fallback response.
    """
    if API_KEY:
        require_api_key()

    payload = get_payload()
    if request.method == "GET" and not payload:
        prompt = request.args.get("prompt") or request.args.get("q") or ""
        payload = {"prompt": prompt} if prompt else {}
    if not isinstance(payload, dict):
        payload = {"_raw": str(payload)}

    prompt = (payload.get("prompt") or payload.get("query") or "") if isinstance(payload, dict) else ""
    if not prompt:
        return jsonify({"ok": False, "error": "missing prompt"}), 400

    # default model selection
    model = payload.get("model", "gpt-4o-mini" if OPENAI_AVAILABLE else "fallback")
    max_tokens = int(payload.get("max_tokens", 512)) if payload.get("max_tokens") is not None else 512
    temperature = float(payload.get("temperature", 0.0)) if payload.get("temperature") is not None else 0.0

    logger.info("NLP request model=%s prompt_len=%d", model, len(prompt))

    # If OpenAI available & key present -> call OpenAI using robust helper
    if OPENAI_KEY and OPENAI_AVAILABLE:
        out = get_openai_response(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
        if out.get("ok"):
            return jsonify({"ok": True, "model": model, "response": out.get("response")}), 200
        else:
            logger.error("OpenAI helper returned error: %s", out.get("error"))
            return jsonify({"ok": False, "error": f"OpenAI call failed: {out.get('error')}", "details": out.get("raw")}), 500

    # Fallback local assistant (deterministic)
    try:
        fallback = (
            f"(fallback) Echoing prompt: {prompt}\n\n"
            "- To enable real responses, set OPENAI_API_KEY in environment variables and ensure 'openai' package is installed.\n"
            "- You can supply JSON: {\"prompt\": \"...\"}, or send raw text in body, or GET /nlp?prompt=...\n"
        )
        return jsonify({"ok": True, "model": "fallback", "response": fallback}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --- Run server ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info("Starting BrittonMethod API on port %s", port)
    logger.info("OPENAI_KEY present=%s (openai package installed=%s)", bool(OPENAI_KEY), OPENAI_AVAILABLE)
    logger.info("BRITTON_API_KEY present=%s", bool(API_KEY))
    app.run(host="0.0.0.0", port=port, threaded=True)
