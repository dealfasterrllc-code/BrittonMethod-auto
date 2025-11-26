# evidence/ledger.py
import os, json, hashlib
from datetime import datetime
MANIFEST_FILE = os.environ.get("EVIDENCE_MANIFEST_FILE", "/tmp/britton_evidence/manifest.jsonl")
os.makedirs(os.path.dirname(MANIFEST_FILE), exist_ok=True)

def build_evidence_item(source: str, raw_bytes: bytes, meta: dict = None) -> dict:
    meta = meta or {}
    sha = hashlib.sha256(raw_bytes).hexdigest()
    filename = f"{sha}.bin"
    local_path = os.path.join(os.path.dirname(MANIFEST_FILE), filename)
    with open(local_path, "wb") as f:
        f.write(raw_bytes)
    item = {"id": sha, "source": source, "sha256": sha, "local_path": local_path, "meta": meta, "timestamp": datetime.utcnow().isoformat()+"Z"}
    with open(MANIFEST_FILE, "a", encoding="utf-8") as mf:
        mf.write(json.dumps(item) + "\n")
    return item

def manifest_from_items():
    items = []
    if not os.path.exists(MANIFEST_FILE):
        return items
    with open(MANIFEST_FILE, "r", encoding="utf-8") as mf:
        for line in mf:
            try:
                items.append(json.loads(line.strip()))
            except Exception:
                continue
    return items
