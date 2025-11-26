# tests/smoke_test.py
import importlib, sys

modules_to_check = [
    "core.scheduler",
    "core.controller",
    "core.logger",
    "modules.ingestion",
    "modules.enrichment",
    "modules.scoring",
    "modules.offer",
    "modules.investor",
    "modules.notifications",
    "modules.api_wrappers.mappedby_api",
    "modules.api_wrappers.attom_api",
]

errors = []
for m in modules_to_check:
    try:
        importlib.import_module(m)
        print(f"[OK] imported {m}")
    except Exception as e:
        errors.append((m, str(e)))
        print(f"[ERR] {m} -> {e}")

if errors:
    print("\nSMOKE TEST FAILED: some modules failed to import.")
    for m, err in errors:
        print(m, err)
    sys.exit(1)

print("\nSMOKE TEST PASSED: all modules imported successfully.")
