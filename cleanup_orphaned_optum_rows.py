#!/usr/bin/env python3
"""
cleanup_orphaned_optum_rows.py
--------------------------------
One-off cleanup for the 2026-10-01 incident: before sync_rates_from_sheets.py
validated ProviderRateOverrides' payer_name, 20 override rows typed
"OPTUM" (Alma) and "OPTUM/UHC/OSCAR" (Headway) instead of the canonical
"Optum/UHC/Oscar" were imported. Since the merge keys on exact string
equality, those rows were treated as brand-new combos rather than
overrides -- the real rate data landed in intermediary_rates under those
wrong, orphaned payer_name values. Nothing is corrupted, it's just
invisible to every query that filters by the real payer_name "Optum/UHC/Oscar".

This script:
  1. Calls GET /api/intermediaries/rows for payer_name in
     {"OPTUM", "OPTUM/UHC/OSCAR"} and PRINTS every row found. Nothing is
     deleted yet.
  2. Only after you confirm (or pass --yes), POSTs those exact rows'
     natural keys to POST /api/intermediaries/delete-superseded.

Run this AFTER you've already:
  (a) fixed the ProviderRateOverrides sheet (payer_name cells corrected
      to "Optum/UHC/Oscar" -- though as of this fix landing, a plain
      "OPTUM" in that column now auto-corrects on its own, so this step
      may already be a no-op), and
  (b) re-run Sync Rates Now (or sync_rates_from_sheets.py directly) and
      confirmed the CORRECT "Optum/UHC/Oscar" rows now exist with the
      right rates.
Deleting the orphaned rows before the correct ones exist would just lose
the data a second time, differently.

Usage:
    python3 cleanup_orphaned_optum_rows.py              # interactive preview + confirm
    python3 cleanup_orphaned_optum_rows.py --yes         # skip the confirmation prompt
    python3 cleanup_orphaned_optum_rows.py --dry-run     # preview only, never deletes
"""
import argparse
import sys

import requests

API_BASE = "http://localhost:8000/api"
ORPHANED_PAYER_NAMES = ["OPTUM", "OPTUM/UHC/OSCAR"]


def find_orphaned_rows() -> list[dict]:
    rows = []
    for payer_name in ORPHANED_PAYER_NAMES:
        resp = requests.get(f"{API_BASE}/intermediaries/rows",
                            params={"payer_name": payer_name}, timeout=30)
        resp.raise_for_status()
        rows.extend(resp.json())
    return rows


def print_rows(rows: list[dict]):
    if not rows:
        print("No rows found under payer_name in", ORPHANED_PAYER_NAMES)
        return
    print(f"{'intermediary':<10} {'payer_name':<18} {'cpt':<7} {'state':<6} "
          f"{'amount':<9} {'provider':<9} {'eff_date':<12} rate_id")
    for r in rows:
        print(f"{r['intermediary_name']:<10} {r['payer_name']:<18} {r['cpt_code']:<7} "
              f"{r['state']:<6} {str(r['allowed_amount']):<9} {str(r.get('provider') or 'COMMON'):<9} "
              f"{str(r.get('effective_date') or ''):<12} {r['rate_id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, never deletes")
    args = parser.parse_args()

    print("Looking up orphaned rows (payer_name in", ORPHANED_PAYER_NAMES, ")...\n")
    rows = find_orphaned_rows()
    print_rows(rows)

    if not rows:
        sys.exit(0)

    if args.dry_run:
        print(f"\n[dry-run] Would delete {len(rows)} row(s) above. Nothing deleted.")
        sys.exit(0)

    if not args.yes:
        answer = input(f"\nDelete these {len(rows)} row(s)? Type 'yes' to confirm: ").strip().lower()
        if answer != "yes":
            print("Aborted. Nothing deleted.")
            sys.exit(1)

    keys = [
        {
            "intermediary_name": r["intermediary_name"],
            "payer_name":        r["payer_name"],
            "cpt_code":          r["cpt_code"],
            "state":             r["state"],
            "provider":          r.get("provider"),
        }
        for r in rows
    ]
    resp = requests.post(f"{API_BASE}/intermediaries/delete-superseded",
                         json={"keys": keys}, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    print(f"\nDelete result: {result['deleted']} deleted, "
          f"{result['not_found']} not found, {len(result['errors'])} error(s)")
    for e in result["errors"]:
        print("  ERROR:", e)


if __name__ == "__main__":
    main()
