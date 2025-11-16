#!/usr/bin/env python3
# Main.py - Britton Method API (production-minded)
import os
import json
import uuid
import time
import traceback
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify, abort
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, Job
import threading
import queue

# Load config from env
API_KEY = os.environ.get("BRITTON_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///britton.db")
REDIS_URL = os.environ.get("REDIS_URL", "")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
HUMAN_IN_LOOP_VALUE = float(os.environ.get("HUMAN_IN_LOOP_VALUE", "50000000"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))

# ensure evidence dir exists
os.makedirs(EVIDENCE_DIR, exist_ok=True)

# Import core functions
from app_core import (
    underwriter_deterministic,
    monte_carlo_simulation,
    simulate_refund_waterfall,
    verify_listing_pipeline,
    store_evidence_binary,
    generate_long_loi_text,
    LLM_PROMPT
)

# personas
try:
    import britton_personas as personas_mod
    BRITTON_PERSONAS = getattr(personas_mod, "BRITTON_PERSONAS", None)
    assign_persona_stack = getattr(personas_mod, "assign_persona_stack", None)
except Exception:
    BRITTON_PERSONAS = None
    assign_persona_stack = None

# DB setup
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)

# in-memory queue fallback (durable queue recommended)
JOB_QUEUE = queue.Queue()
JOB_STORE: Dict[str, Dict[str, Any]] = {}

app = Flask(__name__)

# Helper: API key enforcement
def require_api_key():
    if not API_KEY:
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if not key or key != API_KEY:
        abort(401, description="Missing or invalid API key.")
    return True

# Enqueue job (write to DB + in-memory queue)
def enqueue_job(job_type: str, payload: Dict[str, Any], run_monte_carlo: bool = True, mc_runs: int = 2000) -> str:
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat()+"Z", "status": "queued", "payload": payload}
    JOB_QUEUE.put({"job_id": job_id, "type": job_type, "payload": payload, "monte_carlo": run_monte_carlo, "mc_runs": mc_runs})
    # persist in DB
    try:
        db = SessionLocal()
        job_row = Job(id=job_id, payload=payload, status="queued")
        db.add(job_row)
        db.commit()
        db.close()
    except Exception:
        pass
    return job_id

# background worker (single-thread) - production: replace with RQ/Celery
def worker_loop():
    while True:
        job = JOB_QUEUE.get()
        if job is None:
            break
        job_id = job.get("job_id")
        try:
            JOB_STORE[job_id]["status"] = "running"
            t0 = time.time()
            if job["type"] == "analyze":
                prop = job["payload"]
                det = underwriter_deterministic(prop)
                mc = None
                if job.get("monte_carlo", False):
                    mc = monte_carlo_simulation(prop, runs=job.get("mc_runs", 2000))
                try:
                    raw = json.dumps({"property": prop, "det": det, "mc": mc}).encode("utf-8")
                    ev = store_evidence_binary("analysis_bundle", raw, {"address": prop.get("address")})
                except Exception:
                    ev = None
                result = {"ok": True, "deal": det, "monte_carlo": mc, "evidence": ev}
                # Escalation conditions
                if det.get("britton_score", 0) >= 95 or det.get("price", 0) >= HUMAN_IN_LOOP_VALUE or det.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                    JOB_STORE[job_id]["status"] = "escalated"
                    JOB_STORE[job_id]["result"] = result
                    JOB_STORE[job_id]["escalation"] = {"reason": "human_in_loop_threshold", "created": datetime.utcnow().isoformat()+"Z"}
                else:
                    JOB_STORE[job_id]["status"] = "done"
                    JOB_STORE[job_id]["result"] = result
            elif job["type"] == "verify":
                listing = job["payload"]
                manifest = verify_listing_pipeline(listing)
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = manifest
            elif job["type"] == "simulate":
                payload = job["payload"]
                sim = simulate_refund_waterfall(float(payload.get("price", 0)), float(payload.get("existing_debt", 0)), float(payload.get("investor_equity_pct", 0.25)))
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = {"ok": True, "simulation": sim}
            else:
                JOB_STORE[job_id]["status"] = "error"
                JOB_STORE[job_id]["result"] = {"ok": False, "error": "unknown job type"}
            t1 = time.time()
            JOB_STORE[job_id]["duration_seconds"] = t1 - t0
            # update DB row
            try:
                db = SessionLocal()
                j = db.query(Job).filter(Job.id == job_id).first()
                if j:
                    j.status = JOB_STORE[job_id]["status"]
                    j.result = JOB_STORE[job_id].get("result")
                    db.commit()
                db.close()
            except Exception:
                pass
        except Exception as e:
            JOB_STORE[job_id]["status"] = "error"
            JOB_STORE[job_id]["result"] = {"ok": False, "error": str(e), "tb": traceback.format_exc()}
        finally:
            JOB_QUEUE.task_done()

_worker_thread = threading.Thread(target=worker_loop, daemon=True)
_worker_thread.start()

# Endpoints
@app.route("/")
def index():
    return "<h1>Britton Method — API</h1><p>See /health, /analyze, /verify, /generate-loi, /waterfall/simulate, /webhook/email, /persona-stack, /diagnostics/run</p>"

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()+"Z", "jobs_total": len(JOB_STORE)})

@app.route("/analyze", methods=["POST"])
def analyze_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    prop = payload.get("property", payload)
    if not prop:
        return jsonify({"ok": False, "error": "missing property"}), 400
    job_id = enqueue_job("analyze", prop, run_monte_carlo=bool(payload.get("run_monte_carlo", True)), mc_runs=int(payload.get("mc_runs", 2000)))
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/verify", methods=["POST"])
def verify_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    listing = payload.get("listing", payload)
    if not listing:
        return jsonify({"ok": False, "error": "missing listing"}), 400
    job_id = enqueue_job("verify", listing, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/generate-loi", methods=["POST"])
def loi_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    prop = payload.get("property", payload)
    if not prop:
        return jsonify({"ok": False, "error": "missing property"}), 400
    for k in ("britton_score", "confidence", "internal_notes", "evidence_manifest"):
        prop.pop(k, None)
    loi = generate_long_loi_text(prop)
    ev = None
    try:
        ev = store_evidence_binary("loi_generated", loi.encode("utf-8"), {"address": prop.get("address")})
    except Exception:
        ev = None
    return jsonify({"ok": True, "loi": loi, "evidence": ev}), 200

@app.route("/waterfall/simulate", methods=["POST"])
def waterfall_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    price = payload.get("price")
    existing_debt = payload.get("existing_debt", 0)
    g = payload.get("investor_equity_pct", 0.25)
    if price is None:
        return jsonify({"ok": False, "error": "missing price"}), 400
    job_id = enqueue_job("simulate", {"price": price, "existing_debt": existing_debt, "investor_equity_pct": g}, run_monte_carlo=False)
    return jsonify({"ok": True, "job_id": job_id}), 202

@app.route("/webhook/email", methods=["POST"])
def webhook_email():
    data = request.json or {}
    parsed = data.get("parsed")
    if not parsed:
        return jsonify({"ok": False, "error": "no parsed property"}), 400
    verify_job_id = enqueue_job("verify", parsed, run_monte_carlo=False)
    analysis_job_id = enqueue_job("analyze", parsed, run_monte_carlo=True, mc_runs=2000)
    return jsonify({"ok": True, "verify_job_id": verify_job_id, "analysis_job_id": analysis_job_id}), 202

@app.route("/persona-stack", methods=["POST"])
def persona_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    max_personas = int(payload.get("max_personas", 8))
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

@app.route("/diagnostics/run", methods=["POST"])
def diagnostics_run():
    if API_KEY: require_api_key()
    results = {"time": datetime.utcnow().isoformat()+"Z", "checks": []}
    try:
        sample = b"britton-diagnostics"
        ev = store_evidence_binary("diag_sample", sample, {"note": "diagnostic"})
        results["checks"].append({"name": "evidence_store", "ok": True, "item": {"id": ev.get("id"), "path": ev.get("local_path")}})
    except Exception as e:
        results["checks"].append({"name": "evidence_store", "ok": False, "error": str(e)})
    try:
        results["checks"].append({"name": "llm_prompt_loaded", "ok": bool(LLM_PROMPT)})
    except Exception as e:
        results["checks"].append({"name": "llm_prompt_loaded", "ok": False, "error": str(e)})
    if BRITTON_PERSONAS:
        results["checks"].append({"name": "personas", "ok": True, "count": len(BRITTON_PERSONAS)})
    else:
        results["checks"].append({"name": "personas", "ok": False})
    return jsonify(results), 200

@app.route("/job-status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = JOB_STORE.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify(job), 200

@app.route("/shutdown", methods=["POST"])
def shutdown():
    if API_KEY: require_api_key()
    JOB_QUEUE.put(None)
    return jsonify({"ok": True, "message": "shutdown queued"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
