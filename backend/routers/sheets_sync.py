"""
routers/sheets_sync.py
-----------------------
Handles rate imports from the Solrei MASTER Google Sheet CSV export.

This replaces the old intermediary CSV upload workflow with a smarter parser
that understands the MASTER sheet structure:

  - 26 plan groups × 4 channels (Alma / Headway / Grow / SBH)
  - Provider prefixes: [JJ] = Jodene Jensen, [KR] = Katherine Robins,
                       [LK] = Lori Kistler, no prefix = COMMON (all)
  - SBH column = Direct Submit — stored as intermediary 'SBH'
  - All 26 BCBS plan variants mapped to canonical payer names

Workflow (unchanged from previous):
  1. Office staff update MASTER Google Sheet
  2. Run Apps Script populate_slave_v8.gs (~10 sec)
  3. Download intermediary_rates tab as CSV
  4. Upload here via POST /api/sheets/import
  5. Dashboard updates immediately — no server restart needed

Endpoints:
  POST /api/sheets/import          - upload SLAVE sheet CSV
  GET  /api/sheets/column-map      - returns column→payer+channel mapping (debug)
  GET  /api/channel-comparison-v2  - provider-aware channel comparison
"""

import csv
import io
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse, JSONResponse
from ..database import get_db

router = APIRouter(prefix="/api/sheets", tags=["Sheets Sync"])

# ── Column map: col index → (canonical_payer, channel, provider) ────────────
# Based on Admin Manual v4.0 Section 5.3 column mapping.
# Each plan group = 4 cols: Alma(+0) / Headway(+1) / Grow(+2) / SBH(+3)
# Col A (index 0) = State/CPT label; data starts at col B (index 1).

PLAN_GROUPS = [
    # (start_col_B_offset, canonical_payer_name)
    # Offset is 0-based from col B (i.e. col B = offset 0, col C = 1, ...)
    ( 0,  "Aetna"),
    ( 4,  "Cigna"),
    ( 8,  "Optum/UHC/Oscar"),
    (12,  "Carelon Behavioral Health"),
    (16,  "Ambetter"),
    (20,  "Ambetter"),                          # Ambetter Washington — same payer family
    (24,  "BCBS - Florida Blue"),
    (28,  "BCBS - Florida Blue"),               # Florida Blue Medicare Advantage
    (32,  "BCBS - Arizona"),
    (36,  "BCBS - Massachusetts"),
    (40,  "BCBS - Minnesota"),
    (44,  "BCBS - Minnesota"),                  # Minnesota Medicaid
    (48,  "BCBS - Minnesota"),                  # Minnesota Medicaid Advantage
    (52,  "BCBS - Anthem Colorado"),            # Colorado HMO
    (56,  "BCBS - Anthem Colorado"),            # Colorado PPO
    (60,  "BCBS - Anthem Connecticut"),
    (64,  "BCBS - Anthem Indiana"),
    (68,  "BCBS - Anthem Maine"),
    (72,  "BCBS - Anthem Nevada"),
    (76,  "BCBS - Anthem New Hampshire"),
    (80,  "BCBS - Horizon New Jersey"),
    (84,  "BCBS - Independence Pennsylvania"),
    (88,  "BCBS - Premera Washington"),
    (92,  "BCBS - Regence Washington"),
    (96,  "BCBS - Regence Oregon"),
    (100, "BCBS - Wellmark Iowa"),
]

CHANNELS = ["Alma", "Headway", "Grow Therapy", "SBH"]  # +0, +1, +2, +3

# Payer alias normalization — maps sheet payer names → canonical dashboard names
PAYER_ALIASES = {
    "aetna":                    "Aetna",
    "cigna":                    "Cigna",
    "uhc":                      "Optum/UHC/Oscar",
    "optum":                    "Optum/UHC/Oscar",
    "oscar":                    "Optum/UHC/Oscar",
    "united":                   "Optum/UHC/Oscar",
    "carelon":                  "Carelon Behavioral Health",
    "beacon":                   "Carelon Behavioral Health",
    "ambetter":                 "Ambetter",
    "sunshine":                 "Ambetter",
    "florida blue":             "BCBS - Florida Blue",
    "bcbs - florida":           "BCBS - Florida Blue",
    "florida bcbs":             "BCBS - Florida Blue",
    "bcbs of florida":          "BCBS - Florida Blue",
    "bcbs - arizona":           "BCBS - Arizona",
    "bcbs of arizona":          "BCBS - Arizona",
    "anthem bcbs":              "BCBS - Anthem",
    "anthem blue":              "BCBS - Anthem",
    "bcbs - massachusetts":     "BCBS - Massachusetts",
    "bcbs of ma":               "BCBS - Massachusetts",
    "bcbs of massachusetts":    "BCBS - Massachusetts",
    "bcbs - minnesota":         "BCBS - Minnesota",
    "bcbs of minnesota":        "BCBS - Minnesota",
    "horizon":                  "BCBS - Horizon New Jersey",
    "independence":             "BCBS - Independence Pennsylvania",
    "premera":                  "BCBS - Premera Washington",
    "regence":                  "BCBS - Regence",
    "wellmark":                 "BCBS - Wellmark Iowa",
}

CPT_CODES_TRACKED = {
    "99214", "99215", "90833", "90836", "90838",
    "99204", "99205", "90785",
    "98002", "98003", "98006", "98007",
}

def canonicalize_payer(name: str) -> str:
    if not name:
        return name
    lower = name.lower().strip()
    for key, canonical in PAYER_ALIASES.items():
        if key in lower:
            return canonical
    return name.strip()


def parse_provider_prefix(header_cell: str) -> tuple[str, str]:
    """
    Parse a column header like '[JJ] Headway 7/4/26' or 'Alma 03/31/26'.
    Returns (provider_code, channel_name).
    provider_code: 'JJ', 'KR', 'LK', or None (COMMON).
    channel_name:  'Alma', 'Headway', 'Grow Therapy', 'SBH', or 'Unknown'.
    """
    cell = header_cell.strip()

    # Extract provider prefix
    provider = None
    prefix_match = re.match(r'^\[([A-Z]{2,3})\]\s*', cell)
    if prefix_match:
        provider = prefix_match.group(1)
        cell = cell[prefix_match.end():]

    # Extract channel name
    cell_lower = cell.lower()
    if cell_lower.startswith("alma"):
        channel = "Alma"
    elif cell_lower.startswith("headway"):
        channel = "Headway"
    elif cell_lower.startswith("grow"):
        channel = "Grow Therapy"
    elif cell_lower.startswith("sbh"):
        channel = "SBH"
    else:
        channel = "Unknown"

    return provider, channel


def parse_amount(raw: str) -> Optional[float]:
    """Parse '$109.80', '109.80', 'N/A', '' → float or None."""
    if not raw:
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned.upper() in ("N/A", "NA", "-", "—"):
        return None
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


# ── Column map builder ────────────────────────────────────────────────────────

def build_column_map(header_rows: list[list[str]]) -> dict:
    """
    Read the multi-row header structure of the SLAVE sheet CSV and build
    a map: col_index → {payer, channel, provider, effective_date}.

    The SLAVE sheet header is typically 2 rows:
      Row 1: payer group headers (merged cells exported as repeated values or empty)
      Row 2: channel headers like '[JJ] Headway 7/4/26', 'Alma 03/31/26', 'SBH x/x/xx'
    Col 0 = state/CPT label column.
    """
    col_map = {}

    if len(header_rows) < 2:
        return col_map

    payer_row  = header_rows[0]   # OPTUM, AETNA, CIGNA, ...
    detail_row = header_rows[1]   # [JJ] Headway 7/4/26, Alma 03/31/26, ...

    current_payer = None
    for col_idx, cell in enumerate(detail_row):
        if col_idx == 0:
            continue  # skip label column

        # Update current payer from payer_row (merged cells repeat or are blank)
        if col_idx < len(payer_row):
            pr = payer_row[col_idx].strip().upper()
            # Strip [merged] annotations from the Drive export
            pr = re.sub(r'\[merged\]', '', pr).strip()
            if pr and pr not in ("", "COMMON", "JODENE", "KATIE", "LORI"):
                current_payer = canonicalize_payer(pr)

        if not current_payer:
            continue

        provider, channel = parse_provider_prefix(cell)
        if channel == "Unknown":
            continue

        # Extract effective date if present
        date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', cell)
        eff_date = date_match.group(1) if date_match else None

        col_map[col_idx] = {
            "payer":    current_payer,
            "channel":  channel,
            "provider": provider,   # None = COMMON
            "eff_date": eff_date,
        }

    return col_map


# ── Main import endpoint ──────────────────────────────────────────────────────

@router.post("/import")
async def import_from_sheet(
    file: UploadFile = File(...),
    state: str = Query(default="FL", description="State code for these rates"),
    dry_run: bool = Query(default=False, description="Parse but don't write to DB"),
):
    """
    Upload the SLAVE sheet 'intermediary_rates' tab CSV.

    Steps:
      1. Run Apps Script populate_slave_v8.gs in the SLAVE Google Sheet
      2. File → Download → Comma-separated values (.csv)
      3. Upload here

    The parser reads the multi-row header to build a column→payer/channel/provider
    map, then iterates data rows to extract CPT codes and rates.

    Provider codes stored: JJ, KR, LK, or NULL (COMMON — applies to all).
    SBH channel stored as intermediary 'SBH' (Direct Submit).
    """
    state_upper = (state or "FL").upper()

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── Find data rows ────────────────────────────────────────────────────────
    # The sheet has 2 header rows then data rows starting with state/CPT labels.
    # Strategy: find first row where col 0 looks like a CPT code (5 digits or
    # starts with 9/8) — that's the first data row. Header rows precede it.

    data_start = 0
    header_rows_raw = []
    for i, row in enumerate(all_rows):
        first = (row[0] if row else "").strip()
        if re.match(r'^(99|90|98|97|80|96|H\d|G\d)\d{2,4}$', first):
            data_start = i
            header_rows_raw = all_rows[max(0, i-2):i]
            break

    if data_start == 0 and not header_rows_raw:
        # Fallback: treat first 2 rows as headers
        header_rows_raw = all_rows[:2]
        data_start = 2

    col_map = build_column_map(header_rows_raw)

    if not col_map:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not parse column headers. Make sure you downloaded the "
                "'intermediary_rates' tab (not the MASTER tab) as CSV."
            )
        )

    # ── Load DB references ────────────────────────────────────────────────────
    with get_db() as cur:
        cur.execute("SELECT name, intermediary_id FROM intermediaries WHERE active = TRUE")
        intermediary_map = {r["name"].lower(): r["intermediary_id"] for r in cur.fetchall()}

        cur.execute("SELECT cpt_code FROM cpt_codes")
        valid_cpts = {r["cpt_code"] for r in cur.fetchall()}

    # Verify SBH is registered
    if "sbh" not in intermediary_map:
        raise HTTPException(
            status_code=500,
            detail="SBH intermediary not found in database. Run SQL migration 24 first."
        )

    # ── Parse data rows ───────────────────────────────────────────────────────
    imported = 0
    skipped  = 0
    errors   = []
    preview  = []  # for dry_run

    with get_db() as cur:
        for row_idx, row in enumerate(all_rows[data_start:], start=data_start + 1):
            if not row or not row[0].strip():
                skipped += 1
                continue

            first_cell = row[0].strip()

            # Skip non-CPT rows (state headers, blank separators, notes)
            if not re.match(r'^(99|90|98|97|80|96|H\d|G\d)\d{2,4}$', first_cell):
                skipped += 1
                continue

            cpt_code = first_cell

            # Auto-register unknown CPT codes
            if cpt_code not in valid_cpts:
                try:
                    category = (
                        "Telehealth E/M" if cpt_code.startswith("980") else
                        "Psychiatric"   if cpt_code.startswith("908") else
                        "E/M"
                    )
                    if not dry_run:
                        cur.execute(
                            """
                            INSERT INTO cpt_codes
                                (cpt_code, short_description, category,
                                 is_time_based, is_addon, primary_code_required, telehealth_eligible)
                            VALUES (%s, %s, %s, FALSE, FALSE, FALSE, TRUE)
                            ON CONFLICT (cpt_code) DO NOTHING
                            """,
                            (cpt_code, cpt_code, category),
                        )
                    valid_cpts.add(cpt_code)
                except Exception as e:
                    errors.append(f"Row {row_idx}: could not register CPT {cpt_code} — {e}")
                    skipped += 1
                    continue

            # Iterate columns
            for col_idx, col_info in col_map.items():
                raw = row[col_idx].strip() if col_idx < len(row) else ""
                amount = parse_amount(raw)
                if amount is None:
                    continue

                payer    = col_info["payer"]
                channel  = col_info["channel"]
                provider = col_info["provider"]   # JJ / KR / LK / None
                eff_date = col_info["eff_date"]

                intermediary_id = intermediary_map.get(channel.lower())
                if not intermediary_id:
                    errors.append(f"Row {row_idx}: unknown channel '{channel}' — skipped")
                    continue

                if dry_run:
                    preview.append({
                        "cpt": cpt_code, "payer": payer, "channel": channel,
                        "provider": provider or "COMMON", "amount": amount,
                        "state": state_upper,
                    })
                    imported += 1
                    continue

                # Register payer in mapping table
                try:
                    cur.execute(
                        "INSERT INTO intermediary_payer_map (intermediary_payer_name) "
                        "VALUES (%s) ON CONFLICT DO NOTHING",
                        (payer,),
                    )
                except Exception:
                    pass

                # Upsert rate
                try:
                    cur.execute(
                        """
                        INSERT INTO intermediary_rates
                            (intermediary_id, payer_name, cpt_code, state,
                             allowed_amount, effective_date, provider, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT ON CONSTRAINT intermediary_rates_unique
                        DO UPDATE SET
                            allowed_amount = EXCLUDED.allowed_amount,
                            effective_date = EXCLUDED.effective_date,
                            updated_at     = NOW()
                        """,
                        (
                            intermediary_id, payer, cpt_code, state_upper,
                            amount, eff_date, provider,
                        ),
                    )
                    imported += 1
                except Exception as e:
                    errors.append(
                        f"Row {row_idx} / {payer} / {channel} / {provider}: {str(e)}"
                    )
                    skipped += 1

    result = {
        "status":   "dry_run" if dry_run else "ok",
        "state":    state_upper,
        "imported": imported,
        "skipped":  skipped,
        "errors":   errors[:30],
        "message":  (
            f"{'[DRY RUN] Would import' if dry_run else 'Imported'} "
            f"{imported} rate(s) across "
            f"{len(set(p['payer'] for p in preview) if dry_run else [])} payer(s). "
            f"{skipped} row(s) skipped."
            if dry_run else
            f"Imported {imported} rate(s). {skipped} row(s) skipped."
        ),
    }
    if dry_run:
        result["preview"] = preview[:50]
    return result


# ── Column map debug endpoint ─────────────────────────────────────────────────

@router.post("/column-map")
async def inspect_column_map(file: UploadFile = File(...)):
    """
    Upload a CSV and return the parsed column→payer/channel/provider map.
    Use this to verify the parser is reading your sheet correctly before importing.
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    all_rows = list(csv.reader(io.StringIO(text)))
    header_rows_raw = all_rows[:4]  # show first 4 rows for inspection

    col_map = build_column_map(all_rows[:2])

    return {
        "columns_parsed": len(col_map),
        "header_preview": header_rows_raw,
        "column_map": [
            {
                "col_index": k,
                "payer":    v["payer"],
                "channel":  v["channel"],
                "provider": v["provider"] or "COMMON",
                "eff_date": v["eff_date"],
            }
            for k, v in sorted(col_map.items())
        ],
    }


# ── Provider-aware channel comparison ────────────────────────────────────────

VALID_STATES = {
    "AK", "AZ", "CO", "CT", "DC", "FL", "HI", "ID", "IA", "KS", "ME", "MD",
    "MN", "MT", "NE", "NV", "NH", "NM", "ND", "OR", "SD", "UT", "VT", "WA", "WY",
}


@router.get("/channel-comparison")
def get_channel_comparison_v2(
    state:    str = Query(default="FL"),
    provider: str = Query(default=None,
                          description="JJ, KR, LK, or blank for all (COMMON + specified)"),
    payer:    str = Query(default=None),
    cpt_code: str = Query(default=None),
):
    """
    Provider-aware channel comparison. Returns Alma/Headway/Grow/SBH rates
    filtered by provider and state. COMMON rates apply to all providers.

    SBH column = Direct Submit contracted rate.
    """
    state_upper = (state or "FL").upper()
    if state_upper not in VALID_STATES:
        state_upper = "FL"

    # Build provider filter: include COMMON (NULL) + the requested provider
    provider_filter = ""
    params: dict = {"state": state_upper}

    if provider and provider.upper() in ("JJ", "KR", "LK"):
        provider_filter = "AND (ir.provider IS NULL OR ir.provider = %(provider)s)"
        params["provider"] = provider.upper()
    else:
        provider_filter = ""  # all rows

    payer_filter = ""
    if payer:
        payer_filter = "AND ir.payer_name = %(payer)s"
        params["payer"] = payer

    cpt_filter = ""
    if cpt_code:
        cpt_filter = "AND ir.cpt_code = %(cpt_code)s"
        params["cpt_code"] = cpt_code

    sql = f"""
    WITH
    rates AS (
        SELECT
            ir.payer_name,
            ir.cpt_code,
            ir.state,
            ir.provider,
            i.name                AS channel,
            ir.allowed_amount,
            ir.updated_at,
            ir.effective_date
        FROM intermediary_rates ir
        JOIN intermediaries i ON ir.intermediary_id = i.intermediary_id
        WHERE ir.state = %(state)s
          {provider_filter}
          {payer_filter}
          {cpt_filter}
          AND i.active = TRUE
    ),
    medicare AS (
        SELECT cpt_code, allowed_amount AS medicare_allowed
        FROM   benchmark_fee_schedule
        WHERE  source_name    = 'Medicare 2026'
          AND  effective_year = 2026
          AND  locality       = %(state)s
    ),
    pivoted AS (
        SELECT
            payer_name,
            cpt_code,
            provider,
            MAX(CASE WHEN channel = 'Alma'         THEN allowed_amount END) AS alma_rate,
            MAX(CASE WHEN channel = 'Alma'         THEN updated_at     END) AS alma_updated_at,
            MAX(CASE WHEN channel = 'Headway'      THEN allowed_amount END) AS headway_rate,
            MAX(CASE WHEN channel = 'Headway'      THEN updated_at     END) AS headway_updated_at,
            MAX(CASE WHEN channel = 'Grow Therapy' THEN allowed_amount END) AS grow_rate,
            MAX(CASE WHEN channel = 'Grow Therapy' THEN updated_at     END) AS grow_updated_at,
            MAX(CASE WHEN channel = 'SBH'          THEN allowed_amount END) AS direct_rate,
            MAX(CASE WHEN channel = 'SBH'          THEN updated_at     END) AS direct_updated_at,
            MAX(CASE WHEN channel = 'SBH'          THEN effective_date END) AS direct_effective_date
        FROM rates
        GROUP BY payer_name, cpt_code, provider
    )
    SELECT
        p.payer_name,
        p.cpt_code,
        cc.short_description,
        p.provider,
        m.medicare_allowed,
        p.direct_rate,
        p.direct_updated_at,
        p.direct_effective_date,
        CASE
            WHEN p.direct_rate IS NOT NULL AND m.medicare_allowed > 0
            THEN ROUND((p.direct_rate / m.medicare_allowed * 100)::numeric, 1)
        END AS direct_pct_of_medicare,
        p.headway_rate,    p.headway_updated_at,
        p.alma_rate,       p.alma_updated_at,
        p.grow_rate,       p.grow_updated_at,
        GREATEST(
            COALESCE(p.direct_rate, 0),
            COALESCE(p.headway_rate, 0),
            COALESCE(p.alma_rate, 0),
            COALESCE(p.grow_rate, 0)
        ) AS best_rate,
        CASE
            WHEN GREATEST(
                COALESCE(p.direct_rate, 0),
                COALESCE(p.headway_rate, 0),
                COALESCE(p.alma_rate, 0),
                COALESCE(p.grow_rate, 0)
            ) = 0 THEN 'No Data'
            WHEN COALESCE(p.direct_rate, 0) >= COALESCE(p.headway_rate, 0)
             AND COALESCE(p.direct_rate, 0) >= COALESCE(p.alma_rate, 0)
             AND COALESCE(p.direct_rate, 0) >= COALESCE(p.grow_rate, 0)
             AND p.direct_rate IS NOT NULL
            THEN 'Direct'
            WHEN COALESCE(p.headway_rate, 0) >= COALESCE(p.alma_rate, 0)
             AND COALESCE(p.headway_rate, 0) >= COALESCE(p.grow_rate, 0)
             AND p.headway_rate IS NOT NULL
            THEN 'Headway'
            WHEN COALESCE(p.alma_rate, 0) >= COALESCE(p.grow_rate, 0)
             AND p.alma_rate IS NOT NULL
            THEN 'Alma'
            ELSE 'Grow Therapy'
        END AS best_channel
    FROM pivoted p
    JOIN cpt_codes cc ON cc.cpt_code = p.cpt_code
    LEFT JOIN medicare m ON m.cpt_code = p.cpt_code
    ORDER BY p.payer_name, p.cpt_code, p.provider NULLS FIRST
    """

    with get_db() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
