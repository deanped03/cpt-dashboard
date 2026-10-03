#!/usr/bin/env python3
"""
test_merge_dry_run.py
----------------------
Offline dry-run of build_provider_override_rows() + merge_provider_overrides(),
reusing synthetic grid rows standing in for the real Cigna/FL/Alma and
Headway/Aetna/FL combos used in the earlier dry-run (Dean's examples), PLUS
a deliberately misspelled payer_name row to confirm the new validation
rejects the whole import rather than silently creating a phantom group.

No Sheets/API access -- safe to run anywhere.
"""
import sync_rates_from_sheets as sync


def _header(cols):
    return [list(cols)]


def case(title, grid_rows, override_sheet_rows, expect_error=False):
    print(f"\n=== {title} ===")
    if expect_error:
        try:
            sync.build_provider_override_rows(override_sheet_rows)
            print("FAIL: expected ValueError, none raised")
        except ValueError as e:
            print(f"PASS: rejected as expected:\n  {e}")
        return

    parsed_grid_rows = sync.parse_intermediary_rows(grid_rows) if grid_rows else []
    override_rows = sync.build_provider_override_rows(override_sheet_rows)
    merged, superseded, stats = sync.merge_provider_overrides(parsed_grid_rows, override_rows)
    print("Merge stats:", stats)
    print("Superseded keys:", superseded)
    print("Merged CSV:")
    csv_bytes, _ = sync.rows_to_intermediary_csv(merged)
    print(csv_bytes.decode())


GRID_HEADER = sync.INTERMEDIARY_CSV_HEADER
OVERRIDE_HEADER = sync.PROVIDER_OVERRIDE_HEADER

# ── Case 1: real Cigna/FL/Alma grid (99214 already JJ-tagged, per the
# live data noted in the prior session), full JJ/KR/LK override on 99214.
cigna_grid = _header(GRID_HEADER) + [
    ["Alma", "Cigna", "99214", "FL", "117.00", "2026-09-21", "JJ"],
    ["Alma", "Cigna", "90833", "FL", "73.00",  "2026-09-21", "JJ"],
    ["Alma", "Cigna", "90836", "FL", "91.00",  "2026-09-21", "JJ"],
    ["Alma", "Cigna", "90838", "FL", "124.00", "2026-09-21", "JJ"],
    ["Alma", "Cigna", "99215", "FL", "156.00", "2026-09-21", "JJ"],
]
cigna_override_full = _header(OVERRIDE_HEADER) + [
    ["JJ", "Alma", "Cigna", "FL", "99214", "130.00", "2026-09-30"],
    ["KR", "Alma", "Cigna", "FL", "99214", "110.00", "2026-09-30"],
    ["LK", "Alma", "Cigna", "FL", "99214", "100.00", "2026-09-30"],
]
case("Case 1: full override, already-JJ-tagged original (no stale row expected)",
     cigna_grid, cigna_override_full)

# ── Case 2: same group, only JJ+KR overridden -> LK falls back to $117.
cigna_override_partial = _header(OVERRIDE_HEADER) + [
    ["JJ", "Alma", "Cigna", "FL", "99214", "130.00", "2026-09-30"],
    ["KR", "Alma", "Cigna", "FL", "99214", "110.00", "2026-09-30"],
]
case("Case 2: JJ+KR overridden, LK falls back to original $117", cigna_grid, cigna_override_partial)

# ── Case 3: genuinely blank/COMMON original row -> must be captured as superseded.
headway_grid = _header(GRID_HEADER) + [
    ["Headway", "Aetna", "99214", "FL", "95.00", "2026-09-21", ""],
]
headway_override_full = _header(OVERRIDE_HEADER) + [
    ["JJ", "Headway", "Aetna", "FL", "99214", "130.00", "2026-09-30"],
    ["KR", "Headway", "Aetna", "FL", "99214", "110.00", "2026-09-30"],
    ["LK", "Headway", "Aetna", "FL", "99214", "100.00", "2026-09-30"],
]
case("Case 3: blank/COMMON original -> must appear in superseded_keys",
     headway_grid, headway_override_full)

# ── Case 3b: siblings (different CPT, different state) must NOT be swept in.
headway_grid_siblings = _header(GRID_HEADER) + [
    ["Headway", "Aetna", "99214", "FL", "95.00", "2026-09-21", ""],
    ["Headway", "Aetna", "90833", "FL", "60.00", "2026-09-21", ""],   # sibling CPT, same state
    ["Headway", "Aetna", "99214", "GA", "88.00", "2026-09-21", ""],   # same CPT, sibling state
]
case("Case 3b: sibling CPT/state rows must stay untouched, not superseded",
     headway_grid_siblings, headway_override_full)

# ── Case 4 (the actual 2026-10-01 incident): the misspelled payer_name
# that this whole fix is for. Whole import must be rejected, nothing
# partially merged.
bad_override = _header(OVERRIDE_HEADER) + [
    ["JJ", "Alma", "OPTUM", "FL", "99214", "130.00", "2026-10-01"],
    ["JJ", "Headway", "OPTUM/UHC/OSCAR", "FL", "99214", "130.00", "2026-10-01"],
    ["KR", "Headway", "OPTUM/UHC/OSCAR", "FL", "99214", "110.00", "2026-10-01"],
]
print("\n=== Case 4: the real OPTUM / OPTUM/UHC/OSCAR rows -- must AUTO-CORRECT, not error ===")
corrected = sync.build_provider_override_rows(bad_override)
for r in corrected:
    print(" ", r)
assert all(r["payer_name"] == "Optum/UHC/Oscar" for r in corrected), "FAIL: not all rows corrected"
print("PASS: all rows auto-corrected to 'Optum/UHC/Oscar'")

# ── Case 5: a deliberately misspelled BLUE CROSS payer_name -- must
# hard-error for the WHOLE import (point 3's actual requirement).
bc_typo_override = _header(OVERRIDE_HEADER) + [
    ["JJ", "Alma", "BCBS Texs", "FL", "99214", "130.00", "2026-10-01"],
]
case("Case 5: misspelled Blue Cross payer_name -- whole import must be rejected",
     [], bc_typo_override, expect_error=True)

print("\nAll scenarios completed.")
