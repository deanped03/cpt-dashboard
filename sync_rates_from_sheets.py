#!/usr/bin/env python3
"""
sync_rates_from_sheets.py
--------------------------
Pulls CPT rate data directly from Google Sheets (the "Reimbursement Rates"
Master sheet and its SLAVE sync-output sheet) and loads it into the SolRate
Postgres database via the existing FastAPI import endpoints.

Replaces the manual "export CSV, drag into dashboard" workflow with an
automated pull. The manual upload boxes on the dashboard still work exactly
as before — this script is an additional path into the same endpoints, not
a replacement for them.

Sources of truth (per Dean, 2026-07-27):
  - SLAVE sheet, "intermediary_rates" tab -> POST /api/intermediaries/import
    (this tab already includes SBH direct/clinic-submit rates as one of the
    four "intermediary_name" values, alongside Alma, Headway, Grow Therapy)
  - Master sheet, "Medicare" tab, "PMHNP Expected (85%)" column
    -> POST /api/import-benchmark (one call per state block)
  - Master sheet, "ProviderRateOverrides" tab (2026-09-30) -> merged into
    the intermediary CSV above, before the POST, not its own endpoint.
    See merge_provider_overrides() for the merge rule.

Provider Rate Overrides (2026-09-30):
  A per-provider rate exception (JJ/KR/LK billing a different negotiated
  rate than the group's common one for a specific intermediary/payer/
  state/cpt combo) is entered on the Master sheet's ProviderRateOverrides
  tab, NOT the SLAVE/Apps Script grid. Every override row's payer_name is
  validated before anything is merged or imported -- see
  validate_and_resolve_payer_name() -- because the merge key is exact
  string equality against the grid's payer_name, and a typo'd payer_name
  used to fail silently (orphaned rows under a name nothing else queries
  by; see cleanup_orphaned_optum_rows.py for the 2026-10-01 incident this
  closes).

Run modes:
  Normal (cron / manual):
      python3 sync_rates_from_sheets.py
  Dry run (fetch + parse + print summary, no POST):
      python3 sync_rates_from_sheets.py --dry-run
  Only one piece:
      python3 sync_rates_from_sheets.py --skip-medicare
      python3 sync_rates_from_sheets.py --skip-intermediary
      python3 sync_rates_from_sheets.py --skip-overrides
      python3 sync_rates_from_sheets.py --skip-provider-overrides
  Log to a file (for cron):
      python3 sync_rates_from_sheets.py --log-file ~/cpt_dashboard/logs/rate_sync.log
  Offline self-test of the payer_name validation only (no Sheets/API
  access at all -- safe to run anywhere, including right here):
      python3 sync_rates_from_sheets.py --selftest-payer-validation

Requires:
  pip install --break-system-packages google-api-python-client google-auth requests
"""

import argparse
import csv
import difflib
import io
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Configuration ──────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent

SERVICE_ACCOUNT_FILE = SCRIPT_DIR / ".secrets" / "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

MASTER_SHEET_ID = "1QyfSpVlAba_epE1eehN5wlU1543AGWEpIzmsGPNlgXE"   # "Reimbursement Rates"
SLAVE_SHEET_ID  = "1eSuZtC9gm0vwNftf-sl8GrCTPMExWZ8-ifo8mLRZlpY"   # "Reimbursement Rates — Sync Output (SLAVE)"

INTERMEDIARY_RANGE = "intermediary_rates!A1:G5000"
MEDICARE_RANGE     = "Medicare!A1:H2000"
# ChannelPlanOverrides (2026-08-21): read directly from the MASTER sheet,
# not the SLAVE mirror -- unlike intermediary_rates, this table is small
# (a handful of confirmed cases, not thousands of rate rows) and doesn't
# need the SLAVE sheet's read-optimized mirroring, so there's no Apps
# Script change required to support it. Same pattern already used for the
# Medicare tab, which also reads straight from Master.
OVERRIDE_RANGE = "ChannelPlanOverrides!A1:E200"

# Provider Rate Overrides (2026-09-30): lives on MASTER, same reasoning as
# ChannelPlanOverrides above -- small, hand-maintained table, no SLAVE/Apps
# Script mirroring needed.
PROVIDER_OVERRIDE_RANGE = "ProviderRateOverrides!A1:G500"

# Active provider roster. Not importable from best_channel.py's PROV_MAP --
# that module uses a relative import (`from ..database import get_db`), so
# importing it here would require the full backend package context and
# pull in fastapi/psycopg2/python-dotenv, none of which this otherwise
# dependency-light script needs. Kept as a local constant instead.
# MUST BE KEPT IN SYNC BY HAND with PROV_MAP's values in
# backend/routers/best_channel.py.
PROVIDER_ROSTER = ["JJ", "KR", "LK"]

API_BASE = "http://localhost:8000/api"

VALID_STATES = {
    "AK", "AZ", "CO", "DC", "FL", "HI", "ID", "IA", "KS", "ME", "MD",
    "MN", "MT", "NE", "NV", "NH", "NM", "ND", "OR", "SD", "VT", "WA", "WY",
}

DEFAULT_LOG_PATH = SCRIPT_DIR / "logs" / "rate_sync.log"


# ── Payer name validation (2026-10-01) ─────────────────────────
# Root cause of the 2026-10-01 incident: ProviderRateOverrides' payer_name
# is matched against the grid's payer_name by exact string equality in
# merge_provider_overrides(). "OPTUM" and "OPTUM/UHC/OSCAR" never matched
# the grid's "Optum/UHC/Oscar", so those override rows were treated as
# brand-new combos instead of overrides -- the real rate data landed in
# Postgres under an orphaned payer_name nothing else queries by. See
# cleanup_orphaned_optum_rows.py for the one-off cleanup this caused.
#
# Two different validation strategies, by plan type -- NOT one resolver
# for everything:
#
#   - The 5 "core" plans (Optum/UHC/Oscar, Aetna, Cigna, Carelon Behavioral
#     Health, Ambetter) have a real alias map with lots of real-world
#     variants ("oscar", "oxford", "umr", "aetna - allied plan", ...). A
#     typo/alias here is SAFE to auto-correct, because every alias for a
#     given core plan resolves to exactly one canonical name -- there's no
#     ambiguity to get wrong.
#
#   - The 28 individual Blue Cross plans are a completely different case.
#     best_channel.py's BCBS_BY_STATE fallback exists to guess a PATIENT's
#     plan from Tebra's free-text carrier field when nothing more specific
#     is known -- it resolves by STATE, not by name similarity. Running an
#     override's payer_name through that same machinery would silently
#     "correct" a typo'd or slightly-off Blue Cross plan name to whatever
#     that state's default Blue Cross plan happens to be, which can easily
#     be the WRONG specific plan (e.g. silently landing on Anthem BCBS
#     Colorado when "BCBS CareFirst" was meant). That's worse than today's
#     bug -- today's failure is loud-ish (invisible, but at least inert);
#     a state-fallback "correction" would be quiet and wrong, straight into
#     production rate data. So Blue Cross overrides require an EXACT match
#     against the known 28 plan names -- no fuzzy resolution, no state
#     fallback -- and a non-match is a hard error with a difflib nearest-
#     match suggestion, never a silent guess.
#
# Both canonical lists below are hand-copied from the two real sources of
# truth, not re-derived from whatever currently has a rate entered (which
# could miss a legitimately blank-for-now plan/state combo -- the same
# edge case Code.gs's own docstring calls out for BCBS Nebraska/Florida):
#
#   - CORE_PAYER_ALIASES: every CARRIER_MAP entry in
#     backend/routers/best_channel.py whose value is one of the 5 core
#     canonical names (as of the 2026-08-23 CARRIER_MAP, round 5).
#   - BLUE_CROSS_PLAN_NAMES: Code.gs's BLUE_CROSS_PLANS array verbatim
#     (the Auto-Highlight Best Billing-Method Rate script, MASTER sheet
#     Apps Script -- the fixed-grid rewrite, 2026-07-30).
#
# MUST BE KEPT IN SYNC BY HAND with both of those files -- same caveat as
# PROVIDER_ROSTER/PROV_MAP above. If either source changes (a new Blue
# Cross plan added to the grid, a new alias added to CARRIER_MAP), update
# the matching list here too.

CORE_PAYER_ALIASES = {
    "aetna": "Aetna",
    "aetna - allied plan": "Aetna",
    "aetna - pacificsource": "Aetna",
    "aetna - signature": "Aetna",
    "aetna banner": "Aetna",
    "aetna choice": "Aetna",
    "aetna (headway)": "Aetna",
    "ambetter": "Ambetter",
    "cigna": "Cigna",
    "oscar": "Optum/UHC/Oscar",
    "oxford": "Optum/UHC/Oscar",
    "umr": "Optum/UHC/Oscar",
    "optum": "Optum/UHC/Oscar",
    "united": "Optum/UHC/Oscar",
    "unitedhealthcare": "Optum/UHC/Oscar",
    "united healthcare": "Optum/UHC/Oscar",
    "united health": "Optum/UHC/Oscar",
    "uhc": "Optum/UHC/Oscar",
    "surest": "Optum/UHC/Oscar",
    "medica - united": "Optum/UHC/Oscar",
    "golden rule insurance company": "Optum/UHC/Oscar",
    "golden rule": "Optum/UHC/Oscar",
    "sierra health and life": "Optum/UHC/Oscar",
    "sierra health": "Optum/UHC/Oscar",
    "sierra": "Optum/UHC/Oscar",
    "carelon": "Carelon Behavioral Health",
    "beacon": "Carelon Behavioral Health",
}

CORE_PAYER_NAMES = {
    "Optum/UHC/Oscar", "Aetna", "Cigna", "Carelon Behavioral Health", "Ambetter",
}

BLUE_CROSS_PLAN_NAMES = [
    "Anthem BCBS Colorado", "Anthem BCBS Connecticut", "Anthem BCBS Indiana",
    "Anthem BCBS Maine", "Anthem BCBS Nevada", "Anthem BCBS New Hampshire",
    "Anthem BCBS Virginia", "Anthem Blue Cross California",
    "BCBS Arizona", "BCBS CareFirst", "BCBS Hawaii", "BCBS Massachusetts",
    "BCBS Michigan", "BCBS Minnesota", "BCBS Minnesota Medicaid",
    "BCBS Montana", "BCBS Nebraska", "BCBS Texas", "Blue Shield of California",
    "Florida Blue", "Florida Blue Medicare Advantage", "Horizon BCBS New Jersey",
    "Independence Blue Cross Pennsylvania", "Premera Blue Cross Washington",
    "Providence Health Plan", "Regence BCBS Oregon",
    "Regence BlueShield Washington", "Wellmark Iowa",
]
BLUE_CROSS_PLAN_SET = set(BLUE_CROSS_PLAN_NAMES)

ALL_KNOWN_PAYER_NAMES = sorted(CORE_PAYER_NAMES | BLUE_CROSS_PLAN_SET)


class PayerNameValidationError(ValueError):
    """Raised for an override row whose payer_name can't be safely resolved."""


def _resolve_core_payer_name(raw: str) -> Optional[str]:
    """
    Same three-tier resolution best_channel.py's _resolve_carrier() uses
    (exact match, longest-prefix match, longest-substring match), but run
    only against CORE_PAYER_ALIASES -- never against the Blue Cross/state-
    fallback entries in the real CARRIER_MAP. Returns None if nothing
    matches; never guesses.
    """
    n = (raw or "").lower().strip()
    if not n:
        return None
    if n in CORE_PAYER_ALIASES:
        return CORE_PAYER_ALIASES[n]
    best_key, best_len = None, 0
    for key in CORE_PAYER_ALIASES:
        if n.startswith(key) and len(key) > best_len:
            best_key, best_len = key, len(key)
    if best_key:
        return CORE_PAYER_ALIASES[best_key]
    for key in sorted(CORE_PAYER_ALIASES, key=len, reverse=True):
        if key in n:
            return CORE_PAYER_ALIASES[key]
    return None


def validate_and_resolve_payer_name(raw_payer_name: str) -> tuple[str, bool]:
    """
    Returns (canonical_payer_name, was_corrected).

    - A core-5 alias auto-corrects to its canonical spelling
      (was_corrected=True when the input didn't already match exactly).
    - An exact Blue Cross plan name passes through unchanged.
    - Anything else -- including a near-miss Blue Cross name -- is a hard
      PayerNameValidationError with a difflib nearest-match suggestion.
      Never falls back to BCBS_BY_STATE or any other guess.
    """
    raw = (raw_payer_name or "").strip()
    if not raw:
        raise PayerNameValidationError("payer_name is blank")

    if raw in BLUE_CROSS_PLAN_SET:
        return raw, False

    resolved = _resolve_core_payer_name(raw)
    if resolved:
        return resolved, (resolved != raw)

    suggestions = difflib.get_close_matches(raw, ALL_KNOWN_PAYER_NAMES, n=1, cutoff=0.6)
    hint = f' — did you mean "{suggestions[0]}"?' if suggestions else ""
    raise PayerNameValidationError(
        f'"{raw}" doesn\'t match any known payer{hint}'
    )


# ── Logging setup ──────────────────────────────────────────────

def setup_logging(log_file: Optional[str]):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("rate_sync")


# ── Google Sheets access ──────────────────────────────────────

def get_sheets_service():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError(
            f"Service account key not found at {SERVICE_ACCOUNT_FILE}. "
            "See deployment notes for how to place it."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_values(service, spreadsheet_id: str, range_name: str) -> list[list[str]]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute()
    )
    return result.get("values", [])


# ── Date normalization ─────────────────────────────────────────
# The SLAVE sheet has inconsistent date formatting across rows
# (some cells "2026-07-04", others "7/4/26"). Normalize everything
# to YYYY-MM-DD before it reaches Postgres, rather than trusting
# an ambiguous raw string to parse correctly downstream.

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y")


def normalize_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    log.warning("Could not parse date %r — leaving blank (import endpoint will default it)", raw)
    return None


# ── Intermediary / SBH direct rates ────────────────────────────

INTERMEDIARY_CSV_HEADER = ["intermediary_name", "payer_name", "cpt_code", "state",
                           "allowed_amount", "effective_date", "provider"]


def parse_intermediary_rows(rows: list[list[str]]) -> list[dict]:
    """
    Parse the raw SLAVE intermediary_rates sheet rows into structured
    dicts (one per grid row) -- same header/skip/clean validation
    build_intermediary_csv used to do inline, just split out so the
    Provider Rate Overrides merge can sit between parsing and CSV-writing.
    No behavior change to the parse/validate rules themselves.
    """
    if not rows:
        raise ValueError("intermediary_rates tab returned no rows")

    header = [h.strip().lower() for h in rows[0]]
    if header[:len(INTERMEDIARY_CSV_HEADER)] != INTERMEDIARY_CSV_HEADER:
        raise ValueError(
            f"intermediary_rates header changed — expected {INTERMEDIARY_CSV_HEADER}, got {header}. "
            "Sheet layout may have changed; update this script before proceeding."
        )

    parsed = []
    for raw_row in rows[1:]:
        if not raw_row or not (raw_row[0] or "").strip():
            continue
        padded = raw_row + [""] * (7 - len(raw_row))
        intermediary_name, payer_name, cpt_code, state, allowed_amount, effective_date, provider = padded[:7]

        amount_clean = allowed_amount.replace("$", "").replace(",", "").strip()
        if not amount_clean:
            continue

        parsed.append({
            "intermediary_name": intermediary_name.strip(),
            "payer_name":        payer_name.strip(),
            "cpt_code":          cpt_code.strip(),
            "state":             state.strip().upper(),
            "allowed_amount":    amount_clean,
            "effective_date":    normalize_date(effective_date) or "",
            "provider":          provider.strip().upper(),
        })

    return parsed


def rows_to_intermediary_csv(rows: list[dict]) -> tuple[bytes, int]:
    """
    Write structured intermediary-rate dicts (grid rows, possibly merged
    with Provider Rate Overrides) out as the exact CSV format
    /api/intermediaries/import expects. Column order matches
    INTERMEDIARY_CSV_HEADER exactly -- confirmed directly against
    backend/routers/intermediaries.py's import_rates(), not assumed.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(INTERMEDIARY_CSV_HEADER)
    for row in rows:
        writer.writerow([row.get(col, "") for col in INTERMEDIARY_CSV_HEADER])
    return out.getvalue().encode("utf-8"), len(rows)


def push_intermediary_rates(csv_bytes: bytes, dry_run: bool) -> dict:
    if dry_run:
        log.info("[dry-run] Would POST intermediary_rates CSV (%d bytes)", len(csv_bytes))
        return {"status": "dry-run"}

    resp = requests.post(
        f"{API_BASE}/intermediaries/import",
        files={"file": ("intermediary_rates_sync.csv", csv_bytes, "text/csv")},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


# ── Provider Rate Overrides (2026-09-30) ───────────────────────
# A per-provider rate exception, entered on the Master sheet's
# ProviderRateOverrides tab: provider, intermediary_name, payer_name,
# state, cpt_code, allowed_amount, effective_date. Merged into the
# grid-derived rows above, before the CSV is written/POSTed.

PROVIDER_OVERRIDE_HEADER = ["provider", "intermediary_name", "payer_name",
                            "state", "cpt_code", "allowed_amount", "effective_date"]


def build_provider_override_rows(rows: list[list[str]]) -> list[dict]:
    """
    Parse ProviderRateOverrides using the same conventions as
    parse_intermediary_rows (skip blank/comment rows, validate header,
    pad short rows), plus two hard validations -- collected across every
    row and reported together, never a partial import:

      - provider must be one of PROVIDER_ROSTER (mirrors
        channel_overrides.py's "unknown channel" validation style).
      - payer_name must resolve via validate_and_resolve_payer_name() --
        this is the fix for the 2026-10-01 incident. A core-5 alias gets
        silently auto-corrected to its canonical spelling (logged, not
        hidden); a Blue Cross payer_name must match one of the 28 known
        plans exactly, or the whole import is rejected before anything
        is merged or POSTed.
    """
    if not rows:
        return []

    header = [h.strip().lower() for h in rows[0]]
    if header[:len(PROVIDER_OVERRIDE_HEADER)] != PROVIDER_OVERRIDE_HEADER:
        raise ValueError(
            f"ProviderRateOverrides header changed — expected {PROVIDER_OVERRIDE_HEADER}, "
            f"got {header}. Sheet layout may have changed; update this script before proceeding."
        )

    parsed = []
    errors = []

    for i, raw_row in enumerate(rows[1:], start=2):
        if not raw_row or not (raw_row[0] or "").strip():
            continue
        first_val = (raw_row[0] or "").strip()
        if first_val.startswith("#"):
            continue

        padded = raw_row + [""] * (7 - len(raw_row))
        provider, intermediary_name, payer_name_raw, state, cpt_code, allowed_amount, effective_date = padded[:7]

        provider = provider.strip().upper()
        if provider not in PROVIDER_ROSTER:
            errors.append(
                f"Row {i}: unknown provider '{provider}' — must be exactly "
                f"one of {PROVIDER_ROSTER} — rejected"
            )
            continue

        try:
            canonical_payer_name, was_corrected = validate_and_resolve_payer_name(payer_name_raw)
        except PayerNameValidationError as e:
            errors.append(f"Row {i}: {e}")
            continue

        if was_corrected:
            log.warning("Row %d: payer_name '%s' auto-corrected to '%s'",
                        i, payer_name_raw.strip(), canonical_payer_name)

        amount_clean = allowed_amount.replace("$", "").replace(",", "").strip()
        cpt_code = cpt_code.strip()
        state_clean = state.strip().upper()
        if not amount_clean or not cpt_code or not state_clean:
            errors.append(f"Row {i}: missing allowed_amount/cpt_code/state — rejected")
            continue

        parsed.append({
            "intermediary_name": intermediary_name.strip(),
            "payer_name":        canonical_payer_name,
            "cpt_code":          cpt_code,
            "state":             state_clean,
            "allowed_amount":    amount_clean,
            "effective_date":    normalize_date(effective_date) or "",
            "provider":          provider,
        })

    if errors:
        raise ValueError(
            "ProviderRateOverrides validation failed — nothing imported. "
            + f"{len(errors)} error(s):\n  " + "\n  ".join(errors)
        )

    return parsed


def _group_key(row: dict) -> tuple:
    return (row["intermediary_name"], row["payer_name"], row["state"], row["cpt_code"])


def merge_provider_overrides(
    grid_rows: list[dict], override_rows: list[dict]
) -> tuple[list[dict], list[dict], dict]:
    """
    Group both grid rows and override rows by
    (intermediary_name, payer_name, state, cpt_code).

      - A group with NO override: its grid row(s) pass through unchanged.
      - A group WITH an override: keep the override row(s) as-is; for
        every roster provider who isn't explicitly overridden in that
        group, emit an explicit row for them using the group's original
        (pre-override) rate; drop the original untagged/blank-provider
        row for that group.

    Returns (merged_rows, superseded_keys, stats).

    superseded_keys captures the exact natural key of every original row
    dropped above whose own provider was blank/COMMON (provider ""),
    right at the point the drop decision is made -- nothing inferred
    elsewhere. These get deleted via /api/intermediaries/delete-superseded
    before the main import, since that endpoint is a pure upsert and a
    pre-existing blank-provider row (a different key than any new
    per-provider row) would otherwise sit stale in Postgres forever, and
    best_channel.py's own filter would still match it. A group whose
    original row was already provider-tagged is NOT captured here -- that
    tag either matches an explicit override or gets re-emitted verbatim by
    the roster-fallback loop under its own identical key, so it's never
    actually orphaned.
    """
    grid_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in grid_rows:
        grid_by_key[_group_key(row)].append(row)

    override_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in override_rows:
        override_by_key[_group_key(row)].append(row)

    merged: list[dict] = []
    superseded_keys: list[dict] = []
    groups_overridden = 0
    override_only_groups = 0

    for key, rows_in_group in grid_by_key.items():
        if key not in override_by_key:
            merged.extend(rows_in_group)

    for key, override_rows_in_group in override_by_key.items():
        overridden_providers = {r["provider"] for r in override_rows_in_group}
        merged.extend(override_rows_in_group)

        grid_rows_in_group = grid_by_key.get(key)
        if not grid_rows_in_group:
            override_only_groups += 1
            continue

        groups_overridden += 1
        if len(grid_rows_in_group) > 1:
            log.warning("Group %s has %d grid rows before merge (expected 1) — "
                        "using the first as the fallback/original rate", key, len(grid_rows_in_group))
        original = grid_rows_in_group[0]

        for provider in PROVIDER_ROSTER:
            if provider in overridden_providers:
                continue
            fallback = dict(original)
            fallback["provider"] = provider
            merged.append(fallback)

        # The original row is only ever truly orphaned (never re-emitted
        # under its own key by anything above) when it was itself
        # blank/COMMON -- an already-tagged original's key is identical to
        # whatever re-emits/overrides that same provider.
        if not original["provider"]:
            intermediary_name, payer_name, state, cpt_code = key
            superseded_keys.append({
                "intermediary_name": intermediary_name,
                "payer_name":        payer_name,
                "cpt_code":          cpt_code,
                "state":             state,
                "provider":          None,
            })

    stats = {
        "groups_overridden": groups_overridden,
        "override_only_groups": override_only_groups,
        "superseded_count": len(superseded_keys),
        "total_rows": len(merged),
    }
    return merged, superseded_keys, stats


def push_superseded_deletes(superseded_keys: list[dict], dry_run: bool) -> dict:
    if not superseded_keys:
        return {"deleted": 0, "not_found": 0, "errors": []}
    if dry_run:
        log.info("[dry-run] Would POST %d superseded-row delete key(s): %s",
                 len(superseded_keys), superseded_keys)
        return {"status": "dry-run", "would_delete": len(superseded_keys)}

    resp = requests.post(
        f"{API_BASE}/intermediaries/delete-superseded",
        json={"keys": superseded_keys},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ── Channel plan overrides (2026-08-21) ────────────────────────
# See sql/32_channel_plan_overrides.sql for the full explanation. Same
# build/push pattern as intermediary rates above, just a much smaller,
# separate sheet tab and table -- this only grows when a real cross-
# channel plan substitution is confirmed, not populated speculatively.

def build_override_csv(rows: list[list[str]]) -> tuple[bytes, int]:
    """
    Convert the raw Master Sheet ChannelPlanOverrides rows into the exact
    CSV format /api/channel-overrides/import expects:
      home_plan, channel, effective_plan, notes, active
    """
    if not rows:
        # Not an error -- an empty tab (or one that's just the header row)
        # simply means no overrides are confirmed yet, which is the
        # expected starting state. Return zero rows rather than raising.
        return b"home_plan,channel,effective_plan,notes,active\n", 0

    header = [h.strip().lower() for h in rows[0]]
    expected = ["home_plan", "channel", "effective_plan", "notes", "active"]
    if header[:len(expected)] != expected:
        raise ValueError(
            f"ChannelPlanOverrides header changed — expected {expected}, got {header}. "
            "Sheet layout may have changed; update this script before proceeding."
        )

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(expected)

    row_count = 0
    for raw_row in rows[1:]:
        if not raw_row or not (raw_row[0] or "").strip():
            continue
        padded = raw_row + [""] * (5 - len(raw_row))
        home_plan, channel, effective_plan, notes, active = padded[:5]

        home_plan = home_plan.strip()
        channel = channel.strip()
        effective_plan = effective_plan.strip()
        if not home_plan or not channel or not effective_plan:
            continue

        writer.writerow([home_plan, channel, effective_plan, notes.strip(), active.strip() or "TRUE"])
        row_count += 1

    return out.getvalue().encode("utf-8"), row_count


def push_override_rows(csv_bytes: bytes, dry_run: bool) -> dict:
    if dry_run:
        log.info("[dry-run] Would POST ChannelPlanOverrides CSV (%d bytes)", len(csv_bytes))
        return {"status": "dry-run"}

    resp = requests.post(
        f"{API_BASE}/channel-overrides/import",
        files={"file": ("channel_overrides_sync.csv", csv_bytes, "text/csv")},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ── Medicare benchmark rates ────────────────────────────────────

def parse_medicare_by_state(rows: list[list[str]]) -> dict[str, list[dict]]:
    """
    The Medicare tab has no consistent blank-row separators between state
    blocks, so we can't rely on that. Instead: a row is a "state header" if
    column A is a bare 2-letter code in VALID_STATES and column B (short
    description) is empty. Every row after that, until the next state
    header, belongs to that state.
    """
    if not rows:
        raise ValueError("Medicare tab returned no rows")

    header = [h.strip() for h in rows[0]]
    try:
        code_col = header.index("HCPCS Code")
        pmhnp_col = header.index("PMHNP Expected (85%)")
    except ValueError as e:
        raise ValueError(
            f"Medicare tab header changed — could not find expected columns in {header}. "
            "Update this script before proceeding."
        ) from e

    by_state: dict[str, list[dict]] = {}
    current_state = None

    for raw_row in rows[1:]:
        if not raw_row:
            continue
        padded = raw_row + [""] * (max(code_col, pmhnp_col) + 1 - len(raw_row))
        col_a = (padded[0] or "").strip().upper()
        col_b = (padded[1] or "").strip() if len(padded) > 1 else ""

        if col_a in VALID_STATES and not col_b:
            current_state = col_a
            by_state.setdefault(current_state, [])
            continue

        if current_state is None:
            continue  # rows before the first recognized state header

        cpt_code = (padded[code_col] or "").strip()
        amount_raw = (padded[pmhnp_col] or "").replace("$", "").replace(",", "").strip()
        if not cpt_code or not amount_raw:
            continue
        try:
            amount = float(amount_raw)
        except ValueError:
            log.warning("Skipping unparsable Medicare rate for %s %s: %r",
                        current_state, cpt_code, amount_raw)
            continue

        by_state[current_state].append({"cpt_code": cpt_code, "allowed_amount": amount})

    return by_state


def push_medicare_rates(by_state: dict[str, list[dict]], year: int, dry_run: bool) -> dict:
    source_name = f"Medicare {year}"
    summary = {"states_processed": 0, "total_rates": 0, "errors": []}

    for locality, rates in by_state.items():
        if not rates:
            continue
        payload = {
            "source_name": source_name,
            "locality": locality,
            "effective_year": year,
            "rates": rates,
        }
        if dry_run:
            log.info("[dry-run] Would POST %d Medicare rate(s) for %s", len(rates), locality)
            summary["states_processed"] += 1
            summary["total_rates"] += len(rates)
            continue

        try:
            resp = requests.post(f"{API_BASE}/import-benchmark", json=payload, timeout=60)
            resp.raise_for_status()
            summary["states_processed"] += 1
            summary["total_rates"] += len(rates)
        except requests.RequestException as e:
            log.error("Failed to import Medicare rates for %s: %s", locality, e)
            summary["errors"].append(f"{locality}: {e}")

    return summary


# ── Offline self-test (2026-10-01) ─────────────────────────────
# Exercises validate_and_resolve_payer_name() directly, with no Sheets or
# API access at all -- this is what "re-run the dry-run cases, this time
# including a deliberately misspelled payer_name" actually runs as,
# since there's no live ProviderRateOverrides tab to seed yet.

def _selftest_payer_validation() -> bool:
    cases = [
        # (raw input, expected outcome)
        # Core-5 aliases: auto-corrected silently, no error.
        ("OPTUM",              ("Optum/UHC/Oscar", True)),
        ("OPTUM/UHC/OSCAR",    ("Optum/UHC/Oscar", True)),  # the actual 2026-10-01 incident
        ("oscar",              ("Optum/UHC/Oscar", True)),
        ("Optum/UHC/Oscar",    ("Optum/UHC/Oscar", False)),  # already exact -- not flagged as corrected
        ("aetna - allied plan", ("Aetna", True)),
        ("CIGNA",              ("Cigna", True)),
        ("ambetter",           ("Ambetter", True)),
        ("carelon",            ("Carelon Behavioral Health", True)),
        # Blue Cross: exact match passes through unchanged, no correction.
        ("BCBS Texas",         ("BCBS Texas", False)),
        ("Florida Blue",       ("Florida Blue", False)),
        # Blue Cross: near-miss must hard-error, never silently resolve
        # via a state-based guess.
        ("BCBS Texs",          "ERROR: BCBS Texas"),
        ("Anthem BCBS Colordo", "ERROR: Anthem BCBS Colorado"),
        # Unrecognized garbage: hard error.
        ("Definitely Not A Payer", "ERROR"),
        ("",                   "ERROR"),
    ]

    failures = []
    for raw, expected in cases:
        try:
            result = validate_and_resolve_payer_name(raw)
            if isinstance(expected, tuple):
                if result != expected:
                    failures.append(f"{raw!r}: expected {expected}, got {result}")
                else:
                    log.info("PASS  %-28r -> %s", raw, result)
            else:
                failures.append(f"{raw!r}: expected an error, got {result}")
        except PayerNameValidationError as e:
            if isinstance(expected, tuple):
                failures.append(f"{raw!r}: expected {expected}, got error {e!r}")
            elif expected.startswith("ERROR:") and expected.split("ERROR:", 1)[1].strip() not in str(e):
                failures.append(f"{raw!r}: error message {e!r} missing expected suggestion {expected!r}")
            else:
                log.info("PASS  %-28r -> rejected: %s", raw, e)

    if failures:
        log.error("payer_name validation self-test FAILED (%d/%d):", len(failures), len(cases))
        for f in failures:
            log.error("  %s", f)
        return False

    log.info("payer_name validation self-test: all %d case(s) passed", len(cases))
    return True


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch and parse but do not POST anything")
    parser.add_argument("--skip-intermediary", action="store_true")
    parser.add_argument("--skip-medicare", action="store_true")
    parser.add_argument("--skip-overrides", action="store_true")
    parser.add_argument("--skip-provider-overrides", action="store_true",
                         help="Skip the ProviderRateOverrides merge; import grid rows as-is")
    parser.add_argument("--year", type=int, default=date.today().year,
                         help="Medicare effective year (default: current year)")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--selftest-payer-validation", action="store_true",
                         help="Run offline payer_name validation tests and exit "
                              "(no Sheets/API access needed)")
    args = parser.parse_args()

    if args.selftest_payer_validation:
        setup_logging(None)  # stdout only -- this mode never touches the cron log file
        sys.exit(0 if _selftest_payer_validation() else 1)

    setup_logging(args.log_file)
    log.info("=== Rate sync started %s ===", datetime.now().isoformat(timespec="seconds"))

    try:
        service = get_sheets_service()
    except Exception as e:
        log.error("Could not authenticate to Google Sheets: %s", e)
        sys.exit(1)

    exit_code = 0

    if not args.skip_intermediary:
        try:
            log.info("Fetching SLAVE intermediary_rates tab...")
            raw_rows = fetch_values(service, SLAVE_SHEET_ID, INTERMEDIARY_RANGE)
            grid_rows = parse_intermediary_rows(raw_rows)
            log.info("Parsed %d intermediary/direct rate rows", len(grid_rows))

            merged_rows = grid_rows
            if not args.skip_provider_overrides:
                log.info("Fetching Master ProviderRateOverrides tab...")
                override_raw_rows = fetch_values(service, MASTER_SHEET_ID, PROVIDER_OVERRIDE_RANGE)
                override_rows = build_provider_override_rows(override_raw_rows)
                log.info("Parsed %d provider rate override row(s)", len(override_rows))

                merged_rows, superseded_keys, stats = merge_provider_overrides(grid_rows, override_rows)
                log.info("Provider override merge: %s", stats)

                delete_result = push_superseded_deletes(superseded_keys, args.dry_run)
                log.info("Superseded-row delete result: %s", delete_result)

            csv_bytes, row_count = rows_to_intermediary_csv(merged_rows)
            log.info("Writing %d total intermediary/direct rate rows", row_count)
            result = push_intermediary_rates(csv_bytes, args.dry_run)
            log.info("Intermediary import result: %s", result)
        except Exception as e:
            log.error("Intermediary rate sync failed: %s", e)
            exit_code = 1

    if not args.skip_medicare:
        try:
            log.info("Fetching Master Medicare tab...")
            rows = fetch_values(service, MASTER_SHEET_ID, MEDICARE_RANGE)
            by_state = parse_medicare_by_state(rows)
            log.info("Parsed Medicare rates for %d state(s)", len(by_state))
            result = push_medicare_rates(by_state, args.year, args.dry_run)
            log.info("Medicare import result: %s", result)
            if result.get("errors"):
                exit_code = 1
        except Exception as e:
            log.error("Medicare rate sync failed: %s", e)
            exit_code = 1

    if not args.skip_overrides:
        try:
            log.info("Fetching Master ChannelPlanOverrides tab...")
            rows = fetch_values(service, MASTER_SHEET_ID, OVERRIDE_RANGE)
            csv_bytes, row_count = build_override_csv(rows)
            log.info("Parsed %d channel plan override row(s)", row_count)
            result = push_override_rows(csv_bytes, args.dry_run)
            log.info("Channel override import result: %s", result)
            if result.get("errors"):
                exit_code = 1
        except Exception as e:
            # Deliberately non-fatal to the OTHER two sync steps above, same
            # as they're non-fatal to each other -- and expected to fail
            # loudly the first time, before the ChannelPlanOverrides tab
            # exists on the Master Sheet at all (a missing-tab range request
            # returns a Sheets API error, not an empty result). Once the tab
            # exists this becomes a normal isolated failure like the others.
            log.error("Channel override sync failed: %s", e)
            exit_code = 1

    log.info("=== Rate sync finished (exit code %d) ===", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
