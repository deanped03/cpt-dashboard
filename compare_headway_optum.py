#!/usr/bin/env python3
"""
compare_headway_optum.py
--------------------------
Quick one-off: compares the 10 orphaned Headway/OPTUM-UHC-OSCAR/FL rows
against whatever currently exists under the correct canonical payer_name
"Optum/UHC/Oscar" for the same intermediary/state/cpt/provider combos.
Prints a short table, not raw JSON, so it's cheap to paste back.
"""
import requests

API_BASE = "http://127.0.0.1:8000/api"

ORPHANED = [
    # cpt_code, provider, expected_amount (from the dry-run list)
    ("90833", "JJ", 60.00), ("90833", "KR", 62.73),
    ("90836", "JJ", 76.16), ("90836", "KR", 73.49),
    ("90838", "JJ", 106.00), ("90838", "KR", 102.29),
    ("99214", "JJ", 100.00), ("99214", "KR", 99.40),
    ("99215", "JJ", 140.00), ("99215", "KR", 135.10),
]

resp = requests.get(f"{API_BASE}/intermediaries/rows", params={
    "payer_name": "Optum/UHC/Oscar",
    "intermediary_name": "Headway",
    "state": "FL",
})
resp.raise_for_status()
current = {(r["cpt_code"], r["provider"]): r["allowed_amount"] for r in resp.json()}

print(f"{'cpt':<7} {'provider':<9} {'orphaned $':<12} {'current $':<12} match?")
for cpt, provider, expected in ORPHANED:
    actual = current.get((cpt, provider))
    match = "YES" if actual is not None and abs(float(actual) - expected) < 0.01 else "NO"
    print(f"{cpt:<7} {provider:<9} {expected:<12} {str(actual):<12} {match}")
