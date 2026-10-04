"""
load_medicare_all_states.py  (v2 — corrected 2026-07-22)
----------------------------------------------------------
Calculates and loads 2026 Medicare non-facility (telehealth/office) benchmark
rates for all 25 states where Solrei Behavioral Health has licensed providers.

Formula:
    Payment = (Work_RVU x Work_GPCI + PE_RVU x PE_GPCI + MP_RVU x MP_GPCI) x CF

    CF (Conversion Factor) = $33.40  (2026 non-qualifying APM — verified against
                              CMS CY 2026 PFS Final Rule fact sheet, unchanged
                              from v1)

WHAT CHANGED IN v2 (2026-07-22):
    Every RVU and GPCI value below was replaced with the real, official CY 2026
    figure, extracted programmatically from CMS's own published files (Dean
    downloaded these from cms.gov and they were parsed directly — no manual
    transcription):
        - Addendum B (Relative Value Units) CY 2026 CMS-1832-F, 110325
        - Addendum E (Geographic Practice Cost Indices) CY 2026 CMS-1832-F, 020426

    The previous (v1) hardcoded tables were substantially wrong — verified by
    diff against the files above:
      - GPCI: wrong for essentially every one of the 23 states, not just
        Alaska. Malpractice GPCI was the worst offender (e.g. Minnesota was
        0.634 in v1 vs the real 0.296 — more than double; New Mexico was 0.666
        vs the real 1.201 — less than half).
      - RVUs: wrong for 36 of the 38 codes checked, most severely on the
        psychotherapy-specific codes central to Solrei's business (e.g. 90837
        Work RVU was 2.83 in v1 vs the real 3.78 — 34% low; 90832 was 1.50 vs
        the real 1.94 — 23% low).
      - Two codes in v1 (99354, 99355 — old prolonged-service codes) do not
        exist in the CY 2026 RVU file at all; they were retired. Removed here.
      - Two codes carry a non-"Active" CMS status worth knowing about before
        trusting them as ordinary payable codes: 90846 (status R — bundled/
        restricted) and 99417 (status I — not separately payable under the
        Medicare Physician Fee Schedule; Medicare generally uses G2212 for
        prolonged E/M instead). Left in with a note rather than silently
        dropped — verify intent before relying on either in negotiations.

    For the 5 states with multiple CMS localities (FL, ME, MD, OR, WA), Dean
    confirmed on 2026-07-22 to use the broader "Rest of [State]" locality
    rather than the named-metro locality (Miami/Ft. Lauderdale, Southern
    Maine, Baltimore, Portland, Seattle) as the single state-level
    representative rate. If Solrei's actual patient volume in any of these
    states is concentrated in the named metro instead, edit that state's row
    in GPCI below — the metro-locality numbers are in Addendum E if needed.

    This version still does NOT add locality-level granularity to the
    dashboard itself (one rate per state, as before) — that was an explicit
    scope decision, not an oversight. See project memory for the fuller
    locality-granularity option if priorities change later.

Data sources:
    RVU components : CMS CY 2026 Physician Fee Schedule Final Rule (CMS-1832-F),
                      Addendum B, file dated 110325
    GPCI values    : CMS CY 2026 Physician Fee Schedule Final Rule (CMS-1832-F),
                      Addendum E, file dated 020426
    G-code rates   : Approximate 2026 national rates (unchanged from v1 — no
                      standard RVU breakdown published by CMS for these codes;
                      verify annually against CMS Collaborative Care code tables)

Usage:
    cd "/Users/deanpedersen/Projects/solrei/PROJECT - ACTIVE/Insurance CPT Negotiation Dashboard"
    python3 -m backend.load_medicare_all_states

    # Load a single state only:
    python3 -m backend.load_medicare_all_states --state FL

Options:
    --state XX   Load only the specified state abbreviation (e.g. --state AZ)
    --dry-run    Print calculated rates; do not post to API
    --verbose    Show every CPT rate for every state

Notes:
    - Rates for G0568/G0569/G0570 (CoCM) use fixed national rates because
      CMS has not published standard RVU components for these add-on codes.
    - GPCI and RVU values should be re-verified each January against that
      year's CMS PFS Final Rule Addenda — do not assume this file is still
      correct in a future year without checking.
    - Multiple localities exist within some states; this script uses the
      "Rest of [State]" locality for those (see WHAT CHANGED note above).
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API_URL   = "http://localhost:8000/api/import-benchmark"
CF        = 33.40   # 2026 CMS Conversion Factor (non-qualifying APM) — confirmed correct
YEAR      = 2026

# ---------------------------------------------------------------------------
# RVU components for each CPT code
# Source: CMS CY 2026 PFS Final Rule, Addendum B (file dated 110325)
# Format: (work_rvu, pe_nonfacility_rvu, mp_rvu, short_description)
# ---------------------------------------------------------------------------
RVUS = {
    # -- Evaluation & Management --------------------------------------------
    "99202": (0.93, 1.25, 0.07, "New pt E/M low complexity"),
    "99203": (1.60, 1.76, 0.16, "New pt E/M moderate complexity"),
    "99204": (2.60, 2.47, 0.24, "New pt E/M mod-high complexity"),
    "99205": (3.50, 3.23, 0.36, "New pt E/M high complexity"),
    "99212": (0.70, 1.02, 0.06, "Est pt E/M minimal complexity"),
    "99213": (1.30, 1.46, 0.09, "Est pt E/M low complexity"),
    "99214": (1.92, 2.00, 0.14, "Est pt E/M moderate complexity"),
    "99215": (2.80, 2.75, 0.21, "Est pt E/M high complexity"),

    # -- Psychiatric Diagnostic Evaluations ----------------------------------
    "90791": (3.84, 1.33, 0.02, "Psych eval w/o medical services"),
    "90792": (4.16, 1.72, 0.17, "Psych eval w/ medical services"),

    # -- Individual Psychotherapy (standalone) -------------------------------
    "90832": (1.94, 0.62, 0.01, "Individual therapy 30 min"),
    "90834": (2.56, 0.83, 0.02, "Individual therapy 45 min"),
    "90837": (3.78, 1.20, 0.02, "Individual therapy 60 min"),

    # -- Add-on Psychotherapy with E/M ---------------------------------------
    "90833": (1.71, 0.66, 0.07, "Add-on therapy 30 min with E/M"),
    "90836": (2.17, 0.84, 0.08, "Add-on therapy 45 min with E/M"),
    "90838": (2.86, 1.11, 0.12, "Add-on therapy 60 min with E/M"),

    # -- Family Psychotherapy ------------------------------------------------
    "90846": (2.74, 0.40, 0.03, "Family therapy w/o patient"),  # status R — bundled/restricted, verify before use
    "90847": (2.86, 0.40, 0.02, "Family therapy w/ patient"),

    # -- Group Psychotherapy --------------------------------------------------
    "90853": (0.67, 0.23, 0.01, "Group therapy per patient"),

    # -- Crisis Psychotherapy --------------------------------------------------
    "90839": (3.58, 1.19, 0.03, "Crisis therapy first 60 min"),
    "90840": (1.71, 0.58, 0.02, "Crisis therapy add-on 30 min"),

    # -- Screening --------------------------------------------------------------
    "96127": (0.00, 0.14, 0.01, "Brief behavioral assessment per instrument"),

    # -- Psychological Testing ---------------------------------------------------
    "96130": (2.56, 1.06, 0.09, "Psych testing evaluation first hour"),
    "96136": (0.55, 0.74, 0.02, "Psych testing by NP first 30 min"),
    "96138": (0.00, 1.12, 0.01, "Psych testing by tech first 30 min"),

    # -- Behavioral Health Integration ---------------------------------------------
    "99484": (0.93, 0.73, 0.06, "General BHI 20+ min monthly"),

    # -- Prolonged Services -----------------------------------------------------
    # 99354/99355 retired for CY 2026 — removed (not in Addendum B).
    "99417": (0.61, 0.31, 0.04, "Prolonged E/M add-on per 15 min"),  # status I — not separately payable by Medicare; Medicare typically uses G2212 instead. Verify before relying on this.

    # -- Health Behavior --------------------------------------------------------
    "99406": (0.24, 0.20, 0.02, "Tobacco cessation 3-10 min"),
    "99407": (0.50, 0.33, 0.04, "Tobacco cessation >10 min"),
    "96156": (2.40, 0.80, 0.02, "Health behavior assessment"),
    "96158": (1.66, 0.54, 0.01, "Health behavior intervention individual 30 min"),
    "96159": (0.57, 0.19, 0.00, "Health behavior intervention addl 15 min"),
    "96164": (0.24, 0.10, 0.01, "Health behavior group first 30 min"),
    "96165": (0.11, 0.04, 0.00, "Health behavior group addl 15 min"),
    "96167": (1.77, 0.56, 0.01, "Health behavior family w/ patient 30 min"),
    "96168": (0.63, 0.21, 0.01, "Health behavior family addl 15 min"),
}

# -- CoCM G-codes — fixed national rates (no standard published RVUs) --------
# These codes do not have standard RVU breakdowns published by CMS.
# Same national rate is applied to all states. Verify annually. (unchanged from v1)
GCODES_FIXED = {
    "G0568": (133.20, "CoCM initial month 60+ min; approximate 2026 national rate"),
    "G0569": (100.20, "CoCM subsequent month 30+ min; approximate 2026 national rate"),
    "G0570": ( 39.14, "CoCM additional 30 min add-on; approximate 2026 national rate"),
}

# ---------------------------------------------------------------------------
# 2026 GPCI values for all 23 Solrei states
# Source: CMS CY 2026 PFS Final Rule, Addendum E (file dated 020426)
# Format: state_abbr -> (locality_name, work_gpci, pe_nonfacility_gpci, mp_gpci)
#
# For FL, ME, MD, OR, WA (multi-locality states), "Rest of [State]" was chosen
# per Dean's confirmation 2026-07-22 — see module docstring for how to change
# this to a named metro locality instead.
# ---------------------------------------------------------------------------
GPCI = {
    #       State            Locality description              W_GPCI  PE_GPCI  MP_GPCI
    "AK": ("Alaska",                                            1.500,  1.065,   0.551),
    "AZ": ("Arizona (statewide)",                                1.000,  0.969,   0.856),
    "CO": ("Colorado (Denver locality)",                         1.012,  1.064,   0.781),
    "CT": ("Connecticut (MAC 411201)",                           1.012,  1.065,   0.769),
    "DC": ("Washington DC + MD/VA Suburbs",                      1.054,  1.178,   1.113),
    "FL": ("Florida (Rest of State)",                            1.000,  0.956,   1.503),
    "HI": ("Hawaii, Guam",                                       1.000,  1.137,   0.579),
    "ID": ("Idaho",                                              1.000,  0.920,   0.473),
    "IA": ("Iowa",                                               1.000,  0.915,   0.397),
    "KS": ("Kansas",                                             1.000,  0.904,   0.504),
    "ME": ("Maine (Rest of Maine)",                              1.000,  0.920,   0.622),
    "MD": ("Maryland (Rest of Maryland)",                        1.010,  1.012,   0.918),
    "MN": ("Minnesota",                                          1.000,  1.029,   0.296),
    "MT": ("Montana",                                            1.000,  1.000,   0.998),
    "NE": ("Nebraska",                                           1.000,  0.923,   0.378),
    "NV": ("Nevada",                                             1.000,  1.001,   0.833),
    "NH": ("New Hampshire",                                      1.000,  1.041,   0.875),
    "NM": ("New Mexico",                                         1.000,  0.917,   1.201),
    "ND": ("North Dakota",                                       1.000,  1.000,   0.406),
    "OR": ("Oregon (Rest of Oregon)",                            1.000,  0.996,   0.703),
    "SD": ("South Dakota",                                       1.000,  1.000,   0.336),
    "UT": ("Utah (MAC 350209)",                                  1.000,  0.939,   0.910),
    "VT": ("Vermont",                                            1.000,  0.990,   0.506),
    "WA": ("Washington (Rest of Washington)",                    1.013,  1.053,   0.761),
    "WY": ("Wyoming",                                            1.000,  1.000,   0.740),
}


# ---------------------------------------------------------------------------
# Rate calculation
# ---------------------------------------------------------------------------

def calc_rate(w_rvu, pe_rvu, mp_rvu, w_gpci, pe_gpci, mp_gpci):
    """Calculate Medicare allowed amount using the standard formula."""
    return round((w_rvu * w_gpci + pe_rvu * pe_gpci + mp_rvu * mp_gpci) * CF, 2)


def build_rates_for_state(state, verbose=False):
    """Return the list of rate dicts for a given state."""
    loc_name, w_gpci, pe_gpci, mp_gpci = GPCI[state]
    rates = []

    for cpt, (w, pe, mp, desc) in RVUS.items():
        allowed = calc_rate(w, pe, mp, w_gpci, pe_gpci, mp_gpci)
        notes = (
            f"{desc}; "
            f"Work {w} + PE {pe} + MP {mp} x ${CF:.2f}; "
            f"GPCI W={w_gpci}/PE={pe_gpci}/MP={mp_gpci}"
        )
        rates.append({"cpt_code": cpt, "allowed_amount": allowed, "notes": notes})
        if verbose:
            print(f"    {cpt}  ${allowed:>7.2f}  {desc[:45]}")

    # G-codes: fixed national rate, no GPCI adjustment
    for cpt, (amount, note) in GCODES_FIXED.items():
        rates.append({"cpt_code": cpt, "allowed_amount": amount,
                      "notes": note + f"; no GPCI adjustment applied"})
        if verbose:
            print(f"    {cpt}  ${amount:>7.2f}  (fixed national rate)")

    return rates


# ---------------------------------------------------------------------------
# API posting
# ---------------------------------------------------------------------------

def post_state(state, rates, dry_run=False):
    """POST a state's rates to the benchmark import endpoint."""
    loc_name = GPCI[state][0]
    payload = {
        "source_name": f"Medicare {YEAR}",
        "locality": state,
        "effective_year": YEAR,
        "rates": rates,
    }
    if dry_run:
        print(f"  [DRY RUN] Would POST {len(rates)} rates for {state} ({loc_name})")
        return True

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            imported = result.get("lines_upserted", "?")
            print(f"  [OK] {state}  {imported:>3} rates  ({loc_name})")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"  [FAIL] {state}  HTTP {e.code}: {body[:120]}")
        return False
    except urllib.error.URLError as e:
        print(f"  [FAIL] {state}  Connection error: {e.reason}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Load Medicare 2026 benchmark rates for all Solrei states."
    )
    parser.add_argument(
        "--state",
        metavar="XX",
        help="Load only this state (e.g. --state FL). Default: all 25 states.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate rates but do NOT post to API.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every CPT rate for every state.",
    )
    args = parser.parse_args()

    if args.state:
        state_upper = args.state.upper()
        if state_upper not in GPCI:
            print(f"Unknown state: {args.state}")
            print(f"  Supported states: {', '.join(sorted(GPCI))}")
            sys.exit(1)
        states_to_load = [state_upper]
    else:
        states_to_load = sorted(GPCI)

    total_codes = len(RVUS) + len(GCODES_FIXED)
    mode_label  = " [DRY RUN]" if args.dry_run else ""

    print()
    print(f"Medicare {YEAR} Benchmark Rate Loader (v2 — CMS-verified){mode_label}")
    print(f"{'-'*55}")
    print(f"  Conversion Factor : ${CF}")
    print(f"  CPT codes         : {total_codes} ({len(RVUS)} GPCI-adjusted + {len(GCODES_FIXED)} fixed G-codes)")
    print(f"  States to load    : {len(states_to_load)}")
    print()

    ok_count   = 0
    fail_count = 0

    for state in states_to_load:
        loc_name = GPCI[state][0]
        if args.verbose:
            print(f"  -- {state}: {loc_name}")
        rates = build_rates_for_state(state, verbose=args.verbose)
        success = post_state(state, rates, dry_run=args.dry_run)
        if success:
            ok_count += 1
        else:
            fail_count += 1

    print()
    print(f"{'-'*55}")
    if args.dry_run:
        print(f"  DRY RUN complete. {ok_count} state(s) would have been loaded.")
    else:
        print(f"  Done. {ok_count} succeeded, {fail_count} failed.")
        if fail_count == 0:
            print(f"  All {ok_count} states loaded into benchmark_fee_schedule.")
            print()
            print("  Next: refresh the dashboard and choose a state from the")
            print("  state selector to see GPCI-adjusted Medicare benchmarks.")
    print()


if __name__ == "__main__":
    main()
