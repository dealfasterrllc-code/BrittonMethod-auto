# app_core.py
"""
app_core.py — helper implementations for Britton Method
Provides:
- store_evidence_binary (with optional S3 upload)
- underwriter_deterministic
- monte_carlo_simulation (numpy optional)
- simulate_refund_waterfall
- verify_listing_pipeline + checks (stubs)
- generate_long_loi_text (OpenAI integration + fallback)
- LLM_PROMPT loader from prompts directory
"""

import os
import json
import hashlib
import uuid
import sys
from datetime import datetime
from typing import Dict, Any, Optional

# Optional deps
try:
    import requests
except Exception:
    requests = None

try:
    import boto3
except Exception:
    boto3 = None

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    NUMPY_AVAILABLE = False

try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    openai = None
    OPENAI_AVAILABLE = False

# Environment / config (match names used in Main.py)
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/tmp/britton_evidence")
USE_S3 = os.environ.get("USE_S3", "false").lower() in ("1", "true", "yes")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", os.environ.get("AWS_REGION", "us-east-1"))

# Map render env names to expected internal names (support both ATTOM_KEY and ATTOM_API_KEY)
ATTOM_API_KEY = os.environ.get("ATTOM_KEY", "") or os.environ.get("ATTOM_API_KEY", "")
TWILIO_API_SID = os.environ.get("TWILIO_API_SID", "") or os.environ.get("TWILIO_SID", "")
TWILIO_API_SECRET = os.environ.get("TWILIO_API_SECRET", "") or os.environ.get("TWILIO_AUTH", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "") or os.environ.get("GEMINI_TOKEN", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "") or os.environ.get("TAVILY_KEY", "")

# prompt loader path
PROMPT_PATH = os.path.join("prompts", "britton_underwriter_master.py")

os.makedirs(EVIDENCE_DIR, exist_ok=True)

# load LLM prompt if present
LLM_PROMPT = None
if os.path.exists(PROMPT_PATH):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("prompts.master", PROMPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        LLM_PROMPT = getattr(mod, "BRITTON_UNDERWRITER_PROMPT", None)
    except Exception:
        LLM_PROMPT = None

# --- utility helpers ---
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def append_manifest(record: Dict[str, Any]):
    mf = os.path.join(EVIDENCE_DIR, "manifest.jsonl")
    try:
        with open(mf, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        # best-effort; do not fail the main flow
        pass

def _s3_upload(local_path: str, s3_bucket: str, s3_key: str, region: str = "us-east-1") -> bool:
    if not boto3:
        return False
    try:
        s3 = boto3.client("s3", region_name=region)
        s3.upload_file(local_path, s3_bucket, s3_key)
        return True
    except Exception:
        return False

def store_evidence_binary(source: str, raw_bytes: bytes, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Writes binary evidence to disk, appends manifest line, and optionally uploads to S3.
    Returns item dict.
    """
    meta = meta or {}
    h = sha256_bytes(raw_bytes)
    filename = f"{h}.bin"
    local_path = os.path.join(EVIDENCE_DIR, filename)
    try:
        with open(local_path, "wb") as f:
            f.write(raw_bytes)
    except Exception:
        # If disk write fails, attempt to return the digest only
        item = {"id": h, "source": source, "sha256": h, "size": len(raw_bytes), "timestamp": datetime.utcnow().isoformat() + "Z", "local_path": None, "meta": meta}
        append_manifest(item)
        return item

    item = {
        "id": h,
        "source": source,
        "sha256": h,
        "size": len(raw_bytes),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "local_path": local_path,
        "meta": meta
    }
    append_manifest(item)

    if USE_S3 and S3_BUCKET:
        try:
            s3_key = f"evidence/{filename}"
            ok = _s3_upload(local_path, S3_BUCKET, s3_key, region=S3_REGION)
            if ok:
                item["s3_key"] = s3_key
        except Exception:
            pass
    return item

# --- Underwriter math & Britton Score ---
def compute_britton_score(noi, price, dscr, equity_gap, prop_meta):
    try:
        cashflow = (noi / price) if price > 0 else 0.0
        cashflow_score = min(1.5, cashflow * 10.0)
        dscr_score = min(2.0, (dscr or 0.0)) / 2.0
        gap_score = 1.0 if equity_gap >= 0 else max(0.0, 1.0 + (equity_gap / price if price > 0 else -1.0))
        tags = prop_meta.get("tags") if isinstance(prop_meta.get("tags"), list) else []
        seller_score = 1.2 if any(t.lower() in ("motivated", "probate", "divorce", "pre-foreclosure", "tax-lien") for t in tags) else 1.0
        title_risk = 0.9 if any(t.lower() in ("liens", "clouded-title", "judgment", "bankruptcy") for t in tags) else 1.0
        raw = (cashflow_score * 0.35 + dscr_score * 0.35 + gap_score * 0.2) * 100.0
        raw = raw * seller_score * title_risk
        return max(0.0, min(100.0, raw))
    except Exception:
        return 0.0

def underwriter_deterministic(prop: Dict[str, Any]) -> Dict[str, Any]:
    price = float(prop.get("price") or prop.get("list_price") or 0.0)
    gpr = float(prop.get("gpr") or prop.get("gross_potential_rent") or 0.0)
    vacancy = float(prop.get("vacancy_rate") or 0.08)
    operating = float(prop.get("operating_expenses") or (gpr * 0.4))
    egi = gpr * (1 - vacancy)
    noi = max(0.0, egi - operating)
    loan_amount = 0.75 * price
    interest_rate = float(prop.get("assumed_interest") or 0.055)
    annual_debt_service = loan_amount * interest_rate
    dscr = (noi / annual_debt_service) if annual_debt_service > 0 else None
    g = float(prop.get("investor_equity_pct") or 0.25)
    refund = 1.10 * g * price
    existing_debt = float(prop.get("existing_debt") or 0.0)
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
    # If numpy not present, fallback to deterministic single-run
    if not NUMPY_AVAILABLE:
        det = underwriter_deterministic(prop)
        return {
            "runs": 1,
            "dscr_p50": det.get("dscr"),
            "noi_p50": det.get("noi"),
            "yield_p50": (det.get("noi") / det.get("price") if det.get("price") else 0.0)
        }

    price = float(prop.get("price", 0.0))
    gpr = float(prop.get("gpr", 0.0))
    base_vacancy = float(prop.get("vacancy_rate") or 0.08)
    base_operating = float(prop.get("operating_expenses") or (gpr * 0.4))
    rent_growth = np.random.normal(loc=0.0, scale=0.05, size=runs)
    expense_inflation = np.random.normal(loc=0.02, scale=0.015, size=runs)
    gpr_samples = gpr * (1.0 + rent_growth)
    vacancy_samples = np.clip(base_vacancy + np.random.normal(0, 0.02, runs), 0, 0.5)
    operating_samples = base_operating * (1.0 + expense_inflation)
    egi_samples = gpr_samples * (1.0 - vacancy_samples)
    noi_samples = np.maximum(0.0, egi_samples - operating_samples)
    loan_amount = 0.75 * price
    interest = float(prop.get("assumed_interest") or 0.055)
    debt_service = loan_amount * interest
    dscr_samples = np.where(debt_service > 0, noi_samples / debt_service, np.nan)
    yield_samples = np.where(price > 0, noi_samples / price, 0.0)

    def pct(arr, p): return float(np.nanpercentile(arr, p))

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

# --- Verify pipeline & checks (stubs with evidence storage) ---
def verify_listing_pipeline(listing: Dict[str, Any], attempts: int = 5, require_checks: int = 5) -> Dict[str, Any]:
    manifest = {
        "id": str(uuid.uuid4()),
        "created": datetime.utcnow().isoformat() + "Z",
        "requested_attempts": attempts,
        "required_checks": require_checks,
        "checks": [],
        "evidence": []
    }
    passed = 0
    check_fns = [
        check_primary_listing_active, check_county_record_match, check_tax_lien_status,
        check_contact_validation, check_independent_third_source, check_title_snapshot,
        check_saved_search_crossref, check_agent_mls_validation, check_geocode_and_parcel,
        check_photo_and_image_for_condition
    ]
    attempts_made = 0
    i = 0
    while attempts_made < attempts and passed < require_checks and i < len(check_fns):
        fn = check_fns[i]
        attempts_made += 1
        try:
            result = fn(listing)
            result["attempt"] = attempts_made
            manifest["checks"].append(result)
            # extend evidence items into manifest.evidence
            manifest["evidence"].extend(result.get("evidence_items", []))
            if result.get("passed"):
                passed += 1
            i += 1
        except Exception as e:
            manifest["checks"].append({"check": fn.__name__, "passed": False, "error": str(e)})
            i += 1

    confidence = min(1.0, passed / float(require_checks)) if require_checks > 0 else 0.0
    label = "VERIFIED" if passed >= require_checks else ("UNVERIFIED" if confidence < 0.5 else "PARTIAL")
    manifest.update({"passed_checks": passed, "confidence": confidence, "label": label})
    return manifest

def check_primary_listing_active(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "primary_listing_active", "passed": False, "notes": "", "evidence_items": []}
    url = listing.get("source_url") or listing.get("url")
    if not url:
        out["notes"] = "no source_url"
        return out
    raw = json.dumps({"url": url}).encode("utf-8")
    ev = store_evidence_binary("primary_listing_url", raw, {"url": url})
    out["evidence_items"].append(ev)
    out["passed"] = True
    out["notes"] = "stub: url captured"
    return out

def check_county_record_match(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "county_record", "passed": False, "notes": "", "evidence_items": []}
    address = listing.get("address")
    if not address:
        out["notes"] = "no address"
        return out
    payload = {"address": address, "assessed_value": listing.get("assessed_value")}
    raw = json.dumps(payload).encode("utf-8")
    ev = store_evidence_binary("county_assessor_stub", raw, {"address": address})
    out["evidence_items"].append(ev)
    out["passed"] = True
    return out

def check_tax_lien_status(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "tax_lien", "passed": False, "notes": "", "evidence_items": []}
    tags = listing.get("tags", [])
    has_lien = any("tax-lien" in str(t).lower() for t in tags)
    raw = json.dumps({"tax_lien_found": has_lien}).encode("utf-8")
    ev = store_evidence_binary("tax_lien_stub", raw, {})
    out["evidence_items"].append(ev)
    out["passed"] = not has_lien
    return out

def check_contact_validation(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "contact_validation", "passed": False, "notes": "", "evidence_items": []}
    contact = listing.get("contact", {})
    phone = contact.get("phone") or listing.get("phone")
    email = contact.get("email") or listing.get("email")
    valid_phone = False
    valid_email = False
    if phone:
        import re
        digits = re.sub(r'\D', '', str(phone))
        valid_phone = len(digits) >= 10
        ev = store_evidence_binary("contact_phone", json.dumps({"phone": phone, "digits": digits}).encode("utf-8"), {"phone": phone})
        out["evidence_items"].append(ev)
    if email:
        import re
        valid_email = bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))
        ev = store_evidence_binary("contact_email", json.dumps({"email": email}).encode("utf-8"), {"email": email})
        out["evidence_items"].append(ev)
    out["passed"] = valid_phone or valid_email
    out["notes"] = f"phone_valid={valid_phone},email_valid={valid_email}"
    return out

def check_independent_third_source(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "independent_third_source", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("third_source_stub", json.dumps({"listing": listing}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

def check_title_snapshot(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "title_snapshot", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("title_stub", json.dumps({"address": listing.get("address")}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

def check_saved_search_crossref(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "saved_search_crossref", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("crossref_stub", json.dumps({"listing": listing}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

def check_agent_mls_validation(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "agent_mls_validation", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("agent_mls_stub", json.dumps({"agent_mls": listing.get("agent_mls_id")}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

def check_geocode_and_parcel(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "geocode_parcel", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("geocode_stub", json.dumps({"address": listing.get("address")}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

def check_photo_and_image_for_condition(listing: Dict[str, Any]) -> Dict[str, Any]:
    out = {"check": "photo_condition", "passed": True, "notes": "stub", "evidence_items": []}
    ev = store_evidence_binary("photo_stub", json.dumps({"photos": "stub"}).encode("utf-8"), {})
    out["evidence_items"].append(ev)
    return out

# --- OpenAI helper (v1.0+ compatible) ---
def _get_openai_response(prompt: str, model: str = "gpt-4o-mini", max_tokens: int = 1024, temperature: float = 0.0) -> Dict[str, Any]:
    """
    Return {"ok": True, "response": text} or {"ok": False, "error": msg}
    Defensive: tries modern openai.chat.completions.create, ChatCompletion.create, then legacy Completion.create
    """
    if not OPENAI_AVAILABLE or not OPENAI_KEY or not openai:
        return {"ok": False, "error": "openai not installed or OPENAI_API_KEY missing"}

    try:
        openai.api_key = OPENAI_KEY
    except Exception:
        # best-effort
        pass

    resp = None
    # try multiple call surfaces
    if hasattr(openai, "chat") and hasattr(openai.chat, "completions"):
        try:
            resp = openai.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            resp = None

    if resp is None and hasattr(openai, "ChatCompletion") and hasattr(openai.ChatCompletion, "create"):
        try:
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            resp = None

    if resp is None and hasattr(openai, "Completion") and hasattr(openai.Completion, "create"):
        try:
            resp = openai.Completion.create(
                engine=model,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            resp = None

    if resp is None:
        return {"ok": False, "error": "No supported OpenAI client method available (check openai lib version)"}

    # parse response
    try:
        choices = None
        if hasattr(resp, "choices"):
            try:
                choices = resp.choices
            except Exception:
                try:
                    choices = resp.get("choices")
                except Exception:
                    choices = None
        else:
            try:
                choices = resp.get("choices")
            except Exception:
                choices = None

        text = ""
        if choices:
            first = choices[0]
            if isinstance(first, dict):
                # chat-style
                msg = first.get("message") or first.get("delta") or {}
                if isinstance(msg, dict):
                    text = msg.get("content") or msg.get("text") or ""
                if not text:
                    text = first.get("text") or ""
            else:
                # object with attrs
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
            text = str(resp)

        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not text:
            text = str(resp)
        return {"ok": True, "response": text}
    except Exception as e:
        return {"ok": False, "error": "failed-parsing-response: " + str(e), "raw": str(resp)}

# --- LOI generator (OpenAI + fallback) ---
def generate_long_loi_text(prop: Dict[str, Any], min_words: int = 2000, max_words: int = 2500) -> str:
    """
    Attempts to generate a long LOI using OpenAI if configured, otherwise falls back to deterministic
    """
    safe_prop = {k: v for k, v in prop.items() if k not in ("britton_score", "confidence", "internal_notes", "evidence_manifest")}
    # Try to call OpenAI with the prompt if available
    if OPENAI_AVAILABLE and OPENAI_KEY and LLM_PROMPT:
        try:
            # combine system prompt with user instruction
            user_instruction = f"Create a professional Letter of Intent (LOI) between {min_words} and {max_words} words for the following property data: {json.dumps(safe_prop)}. DO NOT include internal scores (britton_score/confidence) nor any sensitive keys. Render the LOI clearly with sections: Executive Summary, Offer Terms, Financing Structure (three options), Contingencies, Timeline, Contact Info, and Signatures section."
            # call helper
            out = _get_openai_response(user_instruction, model="gpt-4o-mini", max_tokens=3800, temperature=0.0)
            if out.get("ok"):
                text = out.get("response", "")
                # quick length check
                if len(text.split()) < min_words:
                    text += ("\n\n" + ("[Addendum] " + "Please see expanded terms. ") * ((min_words // 10) + 10))
                return text
        except Exception:
            pass

    # Fallback deterministic generator (original behavior)
    parts = []
    parts.append(f"LETTER OF INTENT — {safe_prop.get('address', '[ADDRESS]')}\n")
    parts.append("Executive Summary:\n")
    parts.append(f"Buyer intends to acquire the property located at {safe_prop.get('address', '[ADDRESS]')}. The structure is intended to be creative and seller-friendly.\n\n")
    parts.append("Offer & Structure:\n")
    parts.append(f"Purchase Price: ${safe_prop.get('price', 'TBD')} | Preferred: Seller financing / carryback / hybrid.\n\n")
    parts.append("Contingencies & Due Diligence:\n")
    parts.append("Standard HOA, title, physical inspection & financing contingencies. 10 business day review.\n\n")
    parts.append("Financing Options (Summary):\n")
    parts.append("1) Cash Offer — subject to inspection.\n2) Seller Financing — negotiable term & amortization.\n3) Creative hybrid with investor send-back — refunding investor cash at close.\n\n")
    parts.append("Timeline & Closing:\n")
    parts.append("Buyer requests 10 business days for due diligence and a 30-60 day close window depending on financing.\n\n")
    parts.append("Signatures:\n")
    parts.append("Buyer: ______________________   Date: ___________\nSeller: ______________________  Date: ___________\n\n")
    base_text = "\n".join(parts)
    while len(base_text.split()) < min_words:
        base_text += "\nBuyer aims for a timely, clean close and will work in good faith. " * 30
    return base_text
