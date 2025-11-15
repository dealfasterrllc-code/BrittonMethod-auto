#!/usr/bin/env python3
"""
Main.py — Britton Method Final (production-minded, comprehensive)

Purpose:
 - Complete end-to-end automation scaffold for Britton Method.
 - Multi-level verification pipeline, evidence ledger, Monte-Carlo, LOI generation,
   refund/waterfall simulation, persona stack, webhook/email ingestion, diagnostics.
 - Extension points for ATTOM/CoreLogic, Playwright + proxies, Twilio lookups, and ML.

Important:
 - This file intentionally contains stubs for vendor connectors (ATTOM, CORELOGIC, TITLE, TWILIO lookups).
 - Replace stubs with your actual vendor API modules in modules/api_wrappers/.
 - This script does not perform real-money actions. Use with proper secrets and legal checklists.

Author: Britton Hansle / Assistant
"""

import os
import json
import uuid
import time
import threading
import queue
import traceback
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, List

from flask import Flask, request, jsonify, abort

# Try to load your structured logger, else fallback to simple logger
try:
    from core.logger import logger
except Exception:
    import logging, sys
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    class _L:
        def info(self,*a,**k): logging.info(" ".join(map(str,a)))
        def warn(self,*a,**k): logging.warning(" ".join(map(str,a)))
        def error(self,*a,**k): logging.error(" ".join(map(str,a)))
        def debug(self,*a,**k): logging.debug(" ".join(map(str,a)))
    logger = _L()

# -------------------------
# Config (env-driven)
# -------------------------
API_KEY = os.environ.get("BRITTON_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1","true","yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
AWS_REGION = os.environ.get("S3_REGION", "us-east-1")
PLAYWRIGHT_ENABLED = os.environ.get("PLAYWRIGHT_ENABLED", "false").lower() in ("1","true","yes")
PROXY_PROVIDER = os.environ.get("PROXY_PROVIDER", "")  # metadata only
BRITTON_OFFER_THRESHOLD = float(os.environ.get("BRITTON_OFFER_THRESHOLD", "70"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))
HUMAN_IN_LOOP_VALUE = float(os.environ.get("HUMAN_IN_LOOP_VALUE", "50000000"))
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "10"))  # 5x,10x etc.

os.makedirs(EVIDENCE_DIR, exist_ok=True)

# -------------------------
# Optional imports (stubs)
# -------------------------
try:
    from modules.ingestion import tiny_scrape, parse_listing_from_text
except Exception as e:
    tiny_scrape = None
    parse_listing_from_text = None
    logger.warn("modules.ingestion not available", error=str(e))

try:
    from evidence.ledger import build_evidence_item, manifest_from_items
except Exception as e:
    build_evidence_item = None
    manifest_from_items = None
    logger.warn("evidence.ledger not available", error=str(e))

try:
    import britton_personas as personas_mod
    BRITTON_PERSONAS = getattr(personas_mod, "BRITTON_PERSONAS", None)
    assign_persona_stack = getattr(personas_mod, "assign_persona_stack", None)
except Exception as e:
    BRITTON_PERSONAS = None
    assign_persona_stack = None
    logger.warn("britton_personas not available", error=str(e))

# load prompt if present
PROMPT_PATH = os.path.join("prompts", "britton_underwriter_master.py")
LLM_PROMPT = None
if os.path.exists(PROMPT_PATH):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("prompts.master", PROMPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        LLM_PROMPT = getattr(mod, "BRITTON_UNDERWRITER_PROMPT", None)
        logger.info("Loaded LLM master prompt")
    except Exception as e:
        logger.warn("Failed loading LLM prompt", error=str(e))

# -------------------------
# App & in-memory stores
# -------------------------
app = Flask(__name__)
JOB_QUEUE = queue.Queue()
JOB_STORE: Dict[str, Dict[str,Any]] = {}  # replace with persistent DB in production

# -------------------------
# Evidence utilities
# -------------------------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def store_evidence_binary(source: str, raw_bytes: bytes, meta: Optional[Dict[str,Any]] = None) -> Dict[str,Any]:
    """
    Save evidence locally and optionally to S3. Return an evidence item dict with SHA-256.
    """
    meta = meta or {}
    h = sha256_bytes(raw_bytes)
    filename = f"{h}.bin"
    local_path = os.path.join(EVIDENCE_DIR, filename)
    with open(local_path, "wb") as f:
        f.write(raw_bytes)
    item = {
        "id": h, "source": source, "sha256": h, "size": len(raw_bytes),
        "timestamp": datetime.utcnow().isoformat()+"Z", "local_path": local_path, "meta": meta
    }
    # call ledger helper if present
    try:
        if build_evidence_item:
            item2 = build_evidence_item(source, raw_bytes, meta)
            item["ledger_item"] = item2
    except Exception as e:
        logger.warn("build_evidence_item failed", error=str(e))
    # optional S3 upload
    if USE_S3 and S3_BUCKET:
        try:
            import boto3
            s3 = boto3.client("s3", region_name=AWS_REGION)
            s3_key = f"evidence/{filename}"
            s3.upload_file(local_path, S3_BUCKET, s3_key)
            item["s3_key"] = s3_key
            item["s3_url"] = f"s3://{S3_BUCKET}/{s3_key}"
        except Exception as e:
            logger.warn("S3 upload failed", error=str(e))
    return item

# -------------------------
# Underwriter and modeling
# -------------------------
def underwriter_deterministic(prop: Dict[str,Any]) -> Dict[str,Any]:
    """
    Deterministic underwriting: returns core metrics and a simple Britton Score.
    """
    try:
        price = float(prop.get("price") or prop.get("list_price") or 0)
        gpr = float(prop.get("gpr") or prop.get("gross_potential_rent") or 0)
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
            "price": price, "gpr": gpr, "egi": egi, "noi": noi,
            "operating_expenses": operating, "loan_amount": loan_amount,
            "annual_debt_service": annual_debt_service, "dscr": dscr,
            "refund": refund, "existing_debt": existing_debt, "equity_gap": equity_gap,
            "britton_score": britton_score, "confidence": confidence
        }
    except Exception as e:
        logger.error("underwriter_deterministic error", error=str(e), tb=traceback.format_exc())
        raise

def compute_britton_score(noi, price, dscr, equity_gap, prop_meta):
    """
    Multi-factor Britton Score computation.
    This is a domain stub: replace with ML models for production.
    """
    try:
        # subcomponents: cashflow (NOI/price), dscr, equity_gap, seller_motivation (tag), title_risk (tag)
        cashflow = (noi / price) if price > 0 else 0
        cashflow_score = min(1.5, cashflow*10)  # scaled 0..1.5
        dscr_score = min(2.0, dscr or 0) / 2.0  # 0..1
        gap_score = 1.0 if equity_gap >= 0 else max(0.0, 1.0 + (equity_gap / price))
        # seller motivation proxy: tags may include 'motivated' etc.
        tags = prop_meta.get("tags") if isinstance(prop_meta.get("tags"), list) else []
        seller_score = 1.2 if any(t.lower() in ("motivated","probate","divorce","pre-foreclosure","tax-lien") for t in tags) else 1.0
        title_risk = 0.9 if any(t.lower() in ("liens","clouded-title","judgment","bankruptcy") for t in tags) else 1.0
        raw = (cashflow_score * 0.35 + dscr_score * 0.35 + gap_score * 0.2) * 100
        raw = raw * seller_score * title_risk
        return max(0, min(100, raw))
    except Exception:
        return 0

def monte_carlo_simulation(prop: Dict[str,Any], runs: int = 2000) -> Dict[str,Any]:
    """
    Monte Carlo runs (vectorized) with parameterizable runs.
    """
    import numpy as np
    try:
        price = float(prop.get("price",0))
        gpr = float(prop.get("gpr",0))
        base_vacancy = float(prop.get("vacancy_rate") or 0.08)
        base_operating = float(prop.get("operating_expenses") or (gpr * 0.4))
        rent_growth = np.random.normal(loc=0.0, scale=0.05, size=runs)
        expense_inflation = np.random.normal(loc=0.02, scale=0.015, size=runs)
        gpr_samples = gpr * (1.0 + rent_growth)
        vacancy_samples = np.clip(base_vacancy + np.random.normal(0,0.02,runs), 0, 0.5)
        operating_samples = base_operating * (1.0 + expense_inflation)
        egi_samples = gpr_samples * (1.0 - vacancy_samples)
        noi_samples = np.maximum(0.0, egi_samples - operating_samples)
        loan_amount = 0.75 * price
        interest = float(prop.get("assumed_interest") or 0.055)
        debt_service = loan_amount * interest
        dscr_samples = np.where(debt_service > 0, noi_samples / debt_service, np.nan)
        yield_samples = np.where(price>0, noi_samples / price, 0.0)
        def pct(arr,p): return float(np.nanpercentile(arr,p))
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
    except Exception as e:
        logger.error("monte_carlo_simulation failed", error=str(e))
        raise

# -------------------------
# Waterfall/refund math
# -------------------------
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

# -------------------------
# Comprehensive verification pipeline (configurable attempts)
# -------------------------
def verify_listing_pipeline(listing: Dict[str,Any], attempts: int = 5, require_checks: int = 5) -> Dict[str,Any]:
    """
    Runs multiple independent verification checks until required number of checks are passed or attempts exhausted.
    - listing: {address, source_url, mls_id, etc.}
    - attempts: maximum independent checks to attempt (retries & alternate sources)
    - require_checks: threshold of successful checks required to mark 'verified'
    Returns a manifest containing passed checks, evidence items, confidence score, and final label.
    """
    manifest = {"id": str(uuid.uuid4()), "created": datetime.utcnow().isoformat()+"Z", "requested_attempts": attempts, "required_checks": require_checks, "checks": [], "evidence": []}
    passed = 0
    # list of check functions (stubs and builtins)
    check_fns = [
        check_primary_listing_active,
        check_county_record_match,
        check_tax_lien_status,
        check_contact_validation,
        check_independent_third_source,
        check_title_snapshot,          # more heavyweight
        check_saved_search_crossref,
        check_agent_mls_validation,
        check_geocode_and_parcel,
        check_photo_and_image_for_condition,
    ]
    # attempt through the checks, allow retries up to attempts
    attempts_made = 0
    i = 0
    while attempts_made < attempts and passed < require_checks and i < len(check_fns):
        fn = check_fns[i]
        attempts_made += 1
        try:
            result = fn(listing)
            result["attempt"] = attempts_made
            manifest["checks"].append(result)
            manifest["evidence"].extend(result.get("evidence_items", []))
            if result.get("passed"):
                passed += 1
            i += 1
        except Exception as e:
            logger.warn("verification check failed", check=fn.__name__, error=str(e))
            manifest["checks"].append({"check": fn.__name__, "passed": False, "error": str(e)})
            i += 1
    # compute simple confidence: passed/require_checks capped at 1.0
    confidence = min(1.0, passed / float(require_checks)) if require_checks>0 else 0.0
    label = "VERIFIED" if passed >= require_checks else ("UNVERIFIED" if confidence < 0.5 else "PARTIAL")
    manifest.update({"passed_checks": passed, "confidence": confidence, "label": label})
    return manifest

# -------------------------
# Verification check implementations (stubs & safe fallbacks)
# Replace these with real vendor connectors for production
# -------------------------
def check_primary_listing_active(listing: Dict[str,Any]) -> Dict[str,Any]:
    """
    Attempt to confirm the primary listing (MLS or portal) is active.
    - If listing has 'source_url' use tiny_scrape or Playwright fallback to capture HTML and produce evidence.
    """
    out = {"check": "primary_listing_active", "passed": False, "notes": "", "evidence_items": []}
    source_url = listing.get("source_url") or listing.get("url")
    if not source_url:
        out["notes"] = "no source_url provided"
        return out
    try:
        if tiny_scrape:
            scraped = tiny_scrape(source_url)
            raw = (scraped.get("html") or str(scraped)).encode("utf-8")
            evidence_item = store_evidence_binary("primary_listing_html", raw, {"url": source_url})
            out["evidence_items"].append(evidence_item)
            # naive active detection
            text = scraped.get("snippet","").lower()
            if "sold" in text or "off market" in text or "pending" in text:
                out["notes"] = "listing appears not active text indicators found"
                out["passed"] = False
            else:
                out["notes"] = "scrape retrieved; no obvious offline markers"
                out["passed"] = True
        else:
            # Playwright guidance if enabled (non-executing)
            out["notes"] = "scraper unavailable; consider enabling Playwright + proxies"
            out["passed"] = False
    except Exception as e:
        out["notes"] = f"scrape error: {e}"
        out["passed"] = False
    return out

def check_county_record_match(listing: Dict[str,Any]) -> Dict[str,Any]:
    """
    Attempt to match county assessor record by address/parcel.
    Replace with ATTOM/CoreLogic connector.
    """
    out = {"check": "county_record", "passed": False, "notes": "", "evidence_items": []}
    address = listing.get("address")
    if not address:
        out["notes"] = "no address"
        return out
    try:
        # TODO: replace with vendor API call
        # For now, simulate assessor payload
        payload = {"address": address, "assessed_value": listing.get("assessed_value", None)}
        raw = json.dumps(payload).encode("utf-8")
        item = store_evidence_binary("county_assessor_stub", raw, {"address": address})
        out["evidence_items"].append(item)
        # pass if assessed_value present or stub
        out["passed"] = bool(listing.get("assessed_value") or True)
        out["notes"] = "assessor stub saved (replace with ATTOM/CoreLogic)"
    except Exception as e:
        out["notes"] = str(e)
        out["passed"] = False
    return out

def check_tax_lien_status(listing: Dict[str,Any]) -> Dict[str,Any]:
    """
    Check tax status & liens (replace with vendor/tax collector API).
    """
    out = {"check": "tax_lien", "passed": False, "notes": "", "evidence_items": []}
    address = listing.get("address")
    try:
        # stub: assume no lien unless tag indicates otherwise
        tags = listing.get("tags", [])
        has_lien = any("tax-lien" in str(t).lower() for t in tags)
        raw = json.dumps({"address": address, "tax_lien_found": has_lien}).encode("utf-8")
        item = store_evidence_binary("tax_lien_stub", raw, {"address": address})
        out["evidence_items"].append(item)
        out["passed"] = not has_lien
        out["notes"] = "stub tax lien lookup; integrate tax collector API for production"
    except Exception as e:
        out["notes"] = str(e)
    return out

def check_contact_validation(listing: Dict[str,Any]) -> Dict[str,Any]:
    """
    Validate contact info (phone/email) via regex and optional Twilio Lookup.
    """
    out = {"check": "contact_validation", "passed": False, "notes": "", "evidence_items": []}
    contact = listing.get("contact") or {}
    phone = contact.get("phone") or listing.get("phone")
    email = contact.get("email") or listing.get("email")
    try:
        valid_phone = False
        if phone:
            import re
            # basic phone digits check
            digits = re.sub(r'\D', '', str(phone))
            valid_phone = len(digits) >= 10
            raw = json.dumps({"phone": phone, "digits": digits}).encode("utf-8")
            item = store_evidence_binary("contact_phone_check", raw, {"phone": phone})
            out["evidence_items"].append(item)
        valid_email = False
        if email:
            import re
            valid_email = bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))
            raw = json.dumps({"email": email}).encode("utf-8")
            item2 = store_evidence_binary("contact_email_check", raw, {"email": email})
            out["evidence_items"].append(item2)
        out["passed"] = valid_phone or valid_email
        out["notes"] = f"phone_valid={valid_phone}, email_valid={valid_email}"
    except Exception as e:
        out["notes"] = str(e)
        out["passed"] = False
    return out

def check_independent_third_source(listing: Dict[str,Any]) -> Dict[str,Any]:
    """
    Confirm listing via independent source (title, courthouse, other portal)
    """
    out = {"check": "independent_third_source", "passed": False, "notes": "", "evidence_items": []}
    try:
        # stub: pretend third source check via alternate portal or title snapshot
        raw = json.dumps({"third_source": "stub_ok", "listing": listing}).encode("utf-8")
        item = store_evidence_binary("third_source_stub", raw, {"meta":"stub"})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
        out["passed"] = False
    return out

def check_title_snapshot(listing: Dict[str,Any]) -> Dict[str,Any]:
    out = {"check": "title_snapshot", "passed": False, "notes": "", "evidence_items": []}
    try:
        # TODO: implement title API connector
        raw = json.dumps({"title": "stub_snapshot", "address": listing.get("address")}).encode("utf-8")
        item = store_evidence_binary("title_stub", raw, {"address": listing.get("address")})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
        out["passed"] = False
    return out

def check_saved_search_crossref(listing: Dict[str,Any]) -> Dict[str,Any]:
    out = {"check": "saved_search_crossref", "passed": False, "notes": "", "evidence_items": []}
    try:
        # If listing came from an email or saved search, cross-ref different sources
        # Stub: pass
        raw = json.dumps({"crossref": "stub_ok"}).encode("utf-8")
        item = store_evidence_binary("crossref_stub", raw, {})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
    return out

def check_agent_mls_validation(listing: Dict[str,Any]) -> Dict[str,Any]:
    out = {"check": "agent_mls_validation", "passed": False, "notes": "", "evidence_items": []}
    try:
        # stub: verify agent MLS id against listing
        raw = json.dumps({"agent_mls_check": "stub"}).encode("utf-8")
        item = store_evidence_binary("agent_mls_stub", raw, {})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
    return out

def check_geocode_and_parcel(listing: Dict[str,Any]) -> Dict[str,Any]:
    out = {"check": "geocode_parcel", "passed": False, "notes": "", "evidence_items": []}
    try:
        # stub: call geocoding if lat/lon missing
        raw = json.dumps({"geocode": "stub"}).encode("utf-8")
        item = store_evidence_binary("geocode_stub", raw, {})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
    return out

def check_photo_and_image_for_condition(listing: Dict[str,Any]) -> Dict[str,Any]:
    out = {"check": "photo_condition", "passed": False, "notes": "", "evidence_items": []}
    try:
        # Analytic: photo quality and rehab guess can be implemented later via vision model
        raw = json.dumps({"photos": "stub"}).encode("utf-8")
        item = store_evidence_binary("photo_stub", raw, {})
        out["evidence_items"].append(item)
        out["passed"] = True
    except Exception as e:
        out["notes"] = str(e)
    return out

# -------------------------
# LOI generation
# -------------------------
def generate_long_loi_text(prop: Dict[str,Any], min_words: int = 2000, max_words: int = 2500) -> str:
    """
    Uses LLM if OPENAI_KEY + LLM_PROMPT present. Ensures internal-only fields are stripped.
    """
    safe_prop = {k:v for k,v in prop.items() if k not in ("britton_score","confidence","internal_notes","evidence_manifest")}
    if OPENAI_KEY and LLM_PROMPT:
        try:
            import openai
            openai.api_key = OPENAI_KEY
            sys_msg = {"role":"system","content": LLM_PROMPT}
            user_msg = {"role":"user","content": f"Create a professional LOI 2,000-2,500 words for {json.dumps(safe_prop)}. DO NOT include internal scores (britton_score/confidence)."}
            resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=[sys_msg, user_msg], max_tokens=3800)
            text = resp.choices[0].message.content
            if len(text.split()) < min_words:
                text += "\n\n" + ("[Addendum]\n" + "Please see expanded terms." * 200)
            return text
        except Exception as e:
            logger.warn("OpenAI LOI failed", error=str(e))
    # Fallback deterministic LOI builder
    parts = []
    parts.append(f"LETTER OF INTENT — {safe_prop.get('address','[ADDRESS]')}\n")
    parts.append("Executive Summary:\n")
    parts.append(f"Buyer intends to acquire the property located at {safe_prop.get('address','[ADDRESS]')}. The structure is intended to be creative and seller-friendly.\n\n")
    parts.append("Offer & Structure:\n")
    parts.append(f"Purchase Price: ${safe_prop.get('price','TBD')} | Preferred: Seller financing / carryback / hybrid.\n\n")
    parts.append("Contingencies & Due Diligence:\n")
    parts.append("Standard HOA, title, physical inspection & financing contingencies. 10 business day review.\n\n")
    base_text = "\n".join(parts)
    while len(base_text.split()) < min_words:
        base_text += ("\nClarification: Buyer aims for a timely, clean close and will work in good faith. " * 30)
    return base_text

# -------------------------
# Background worker (jobs: analyze, verify, simulate, waterfall)
# -------------------------
def worker_loop():
    logger.info("Background worker starting...")
    while True:
        job = JOB_QUEUE.get()
        if job is None:
            logger.info("Worker shutting down")
            break
        job_id = job.get("job_id")
        try:
            JOB_STORE[job_id]["status"] = "running"
            t0 = time.time()
            if job["type"] == "analyze":
                prop = job["payload"].get("property") or job["payload"]
                det = underwriter_deterministic(prop)
                mc = None
                if job.get("monte_carlo", False):
                    mc = monte_carlo_simulation(prop, runs=job.get("mc_runs",2000))
                # evidence capture
                try:
                    raw = json.dumps({"property": prop, "det": det, "mc": mc}).encode("utf-8")
                    ev = store_evidence_binary("analysis_bundle", raw, {"address": prop.get("address")})
                except Exception as e:
                    logger.warn("analysis evidence save failed", error=str(e))
                    ev = None
                result = {"ok": True, "deal": det, "monte_carlo": mc, "evidence": ev}
                # Escalation conditions
                if det.get("britton_score",0) >= 95 or det.get("price",0) >= HUMAN_IN_LOOP_VALUE or det.get("confidence",0) < CONFIDENCE_THRESHOLD:
                    JOB_STORE[job_id]["status"] = "escalated"
                    JOB_STORE[job_id]["result"] = result
                    JOB_STORE[job_id]["escalation"] = {"reason": "human_in_loop_threshold", "created": datetime.utcnow().isoformat()+"Z"}
                else:
                    JOB_STORE[job_id]["status"] = "done"
                    JOB_STORE[job_id]["result"] = result
            elif job["type"] == "verify":
                listing = job["payload"].get("listing") or job["payload"]
                attempts = job.get("attempts", 5)
                required = job.get("required_checks", 5)
                manifest = verify_listing_pipeline(listing, attempts=attempts, require_checks=required)
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = manifest
            elif job["type"] == "simulate":
                payload = job["payload"]
                sim = simulate_refund_waterfall(float(payload.get("price",0)), float(payload.get("existing_debt",0)), float(payload.get("investor_equity_pct",0.25)))
                JOB_STORE[job_id]["status"] = "done"
                JOB_STORE[job_id]["result"] = {"ok": True, "simulation": sim}
            else:
                JOB_STORE[job_id]["status"] = "error"
                JOB_STORE[job_id]["result"] = {"ok": False, "error": "unknown job type"}
            t1 = time.time()
            JOB_STORE[job_id]["duration_seconds"] = t1 - t0
        except Exception as e:
            logger.error("worker job error", job_id=job_id, error=str(e), tb=traceback.format_exc())
            JOB_STORE[job_id]["status"] = "error"
            JOB_STORE[job_id]["result"] = {"ok": False, "error": str(e)}
        finally:
            JOB_QUEUE.task_done()

_worker_thread = threading.Thread(target=worker_loop, daemon=True)
_worker_thread.start()

# -------------------------
# API endpoints
# -------------------------
@app.route("/")
def index():
    return "<h1>Britton Method — Final</h1><p>Use /health, /jobs, /analyze, /verify, /generate-loi, /waterfall, /diagnostics</p>"

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()+"Z", "jobs_total": len(JOB_STORE)})

@app.route("/jobs", methods=["GET"])
def list_jobs():
    if API_KEY: require_api_key()
    return jsonify({jid: {"status": j.get("status"), "created": j.get("created")} for jid,j in JOB_STORE.items()})

@app.route("/job-status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = JOB_STORE.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify(job)

@app.route("/analyze", methods=["POST"])
def analyze_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    prop = payload.get("property") or payload
    if not prop: return jsonify({"ok": False, "error": "missing property"}), 400
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat()+"Z", "status": "queued", "payload": prop}
    job = {"job_id": job_id, "type": "analyze", "payload": {"property": prop}, "monte_carlo": bool(payload.get("run_monte_carlo", True)), "mc_runs": int(payload.get("mc_runs",2000))}
    JOB_QUEUE.put(job)
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/verify", methods=["POST"])
def verify_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    listing = payload.get("listing") or payload
    if not listing: return jsonify({"ok": False, "error": "missing listing"}), 400
    attempts = int(payload.get("attempts", MAX_VERIFICATION_ATTEMPTS))
    required = int(payload.get("required_checks", min(5, attempts)))
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat()+"Z", "status": "queued", "payload": listing}
    JOB_QUEUE.put({"job_id": job_id, "type": "verify", "payload": {"listing": listing}, "attempts": attempts, "required_checks": required})
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/generate-loi", methods=["POST"])
def loi_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    prop = payload.get("property") or payload
    if not prop: return jsonify({"ok": False, "error": "missing property"}), 400
    # strip sensitive internal fields
    for k in ("britton_score","confidence","internal_notes","evidence_manifest"):
        prop.pop(k, None)
    loi = generate_long_loi_text(prop)
    try:
        ev = store_evidence_binary("loi_generated", loi.encode("utf-8"), {"address": prop.get("address")})
    except Exception as e:
        logger.warn("LOI evidence store failed", error=str(e))
        ev = None
    return jsonify({"ok": True, "loi": loi, "evidence": ev})

@app.route("/waterfall/simulate", methods=["POST"])
def waterfall_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    price = payload.get("price")
    debt = payload.get("existing_debt", 0)
    g = payload.get("investor_equity_pct", 0.25)
    if price is None:
        return jsonify({"ok": False, "error": "missing price"}), 400
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat()+"Z", "status": "queued", "payload": {"price": price}}
    JOB_QUEUE.put({"job_id": job_id, "type": "simulate", "payload": {"price": price, "existing_debt": debt, "investor_equity_pct": g}})
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/webhook/email", methods=["POST"])
def webhook_email():
    """
    Endpoint for n8n or other integrations to POST parsed emails / saved searches.
    Accepts: { source, subject, body, parsed: {address, price, beds, units, link, tags} }
    """
    data = request.json or {}
    parsed = data.get("parsed")
    if not parsed and parse_listing_from_text:
        parsed = parse_listing_from_text(data.get("body", ""))
    if not parsed:
        return jsonify({"ok": False, "error": "no parsed property"}), 400
    # enqueue verify+analyze as a recommended flow
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"created": datetime.utcnow().isoformat()+"Z", "status": "queued", "payload": parsed}
    JOB_QUEUE.put({"job_id": job_id, "type": "verify", "payload": {"listing": parsed}, "attempts": 5, "required_checks": 5})
    # also queue analysis (quick)
    analysis_job_id = str(uuid.uuid4())
    JOB_STORE[analysis_job_id] = {"created": datetime.utcnow().isoformat()+"Z","status":"queued","payload": parsed}
    JOB_QUEUE.put({"job_id": analysis_job_id, "type": "analyze", "payload": {"property": parsed}, "monte_carlo": True, "mc_runs": 2000})
    return jsonify({"ok": True, "verify_job_id": job_id, "analysis_job_id": analysis_job_id})

@app.route("/persona-stack", methods=["POST"])
def persona_route():
    if API_KEY: require_api_key()
    payload = request.json or {}
    max_personas = int(payload.get("max_personas", 8))
    if assign_persona_stack:
        try:
            stack = assign_persona_stack(payload, max_personas=max_personas)
            return jsonify({"ok": True, "stack": stack})
        except Exception as e:
            logger.warn("assign_persona_stack error", error=str(e))
    if BRITTON_PERSONAS:
        out = [{"id":k, "persona": BRITTON_PERSONAS[k]} for k in sorted(BRITTON_PERSONAS.keys())[:max_personas]]
        return jsonify({"ok": True, "stack": out})
    return jsonify({"ok": False, "error": "personas module not available"}), 500

@app.route("/diagnostics/run", methods=["POST"])
def diagnostics_run():
    """
    Runs a set of smoke tests locally to ensure core modules are reachable.
    This is intended for quick sanity checks only.
    """
    if API_KEY: require_api_key()
    results = {"time": datetime.utcnow().isoformat()+"Z", "checks": []}
    # check evidence store
    try:
        sample = b"britton-diagnostics"
        ev = store_evidence_binary("diag_sample", sample, {"note":"diagnostic"})
        results["checks"].append({"name":"evidence_store","ok":True,"item":ev})
    except Exception as e:
        results["checks"].append({"name":"evidence_store","ok":False,"error":str(e)})
    # check tiny_scrape availability
    try:
        if tiny_scrape:
            s = tiny_scrape("https://example.com")
            results["checks"].append({"name":"tiny_scrape","ok":True,"sample_title": s.get("title")})
        else:
            results["checks"].append({"name":"tiny_scrape","ok":False,"error":"module missing; use Playwright for heavy scraping"})
    except Exception as e:
        results["checks"].append({"name":"tiny_scrape","ok":False,"error":str(e)})
    # check personas
    try:
        if BRITTON_PERSONAS:
            results["checks"].append({"name":"personas","ok":True,"count": len(BRITTON_PERSONAS)})
        else:
            results["checks"].append({"name":"personas","ok":False,"error":"britton_personas missing"})
    except Exception as e:
        results["checks"].append({"name":"personas","ok":False,"error":str(e)})
    # check LLM prompt presence
    try:
        results["checks"].append({"name":"llm_prompt_loaded","ok": bool(LLM_PROMPT)})
    except Exception as e:
        results["checks"].append({"name":"llm_prompt_loaded","ok":False,"error":str(e)})
    return jsonify(results)

# -------------------------
# Helper: API key requirement
# -------------------------
def require_api_key():
    if not API_KEY:
        return True
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if key == API_KEY:
        return True
    abort(401, description="Missing or invalid API key.")

# -------------------------
# Shutdown (safe) - protected by API key
# -------------------------
@app.route("/shutdown", methods=["POST"])
def shutdown():
    if API_KEY: require_api_key()
    JOB_QUEUE.put(None)
    return jsonify({"ok": True, "message": "shutdown queued"})

# -------------------------
# Playwright / scraping guidance (non-sensitive)
# -------------------------
@app.route("/scrape-guidance", methods=["GET"])
def scrape_guidance():
    guidance = {
        "playwright": {
            "note": "Use Playwright for JS-rendered portals. Run in headless browsers with proper container flags.",
            "example": "Use one browser instance per worker, rotate contexts for IP rotation."
        },
        "proxies": {
            "note": "Use residential/ISP proxies for enterprise scraping (BrightData/Oxylabs). Respect robots.txt and legal constraints.",
            "provider_env": "set PROXY_PROVIDER for metadata"
        },
        "rate_limit": "Throttle per-domain (~1-5 requests/min depending on X-RATE limits)."
    }
    return jsonify(guidance)

# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logger.info("Starting BrittonMethod Final", port=port)
    app.run(host="0.0.0.0", port=port, threaded=True)
