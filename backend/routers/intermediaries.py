"""
routers/intermediaries.py
--------------------------
Updated to handle v9 SLAVE sheet CSV format:
  - Now reads optional 'provider' column (JJ / KR / LK / blank=COMMON)
  - All four channels in one CSV: Alma, Headway, Grow Therapy, SBH (Direct Submit)
  - Everything else unchanged

CSV format from populate_slave_v9.gs:
  intermediary_name, payer_name, cpt_code, state, allowed_amount, effective_date, provider
"""

import csv
import io
from datetime import date

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from ..database import get_db
from ..models import DeleteSupersededRequest, DeleteSupersededResponse

router = APIRouter(prefix="/api", tags=["Intermediaries"])


# ── List all platforms ────────────────────────────────────────

@router.get("/intermediaries")
def list_intermediaries():
    with get_db() as cur:
        cur.execute(
            """
            SELECT i.intermediary_id, i.name, i.display_name, i.website,
                   i.fee_description, i.notes, i.active,
                   COUNT(ir.rate_id) AS rate_count
            FROM intermediaries i
            LEFT JOIN intermediary_rates ir ON i.intermediary_id = ir.intermediary_id
            GROUP BY i.intermediary_id, i.name, i.display_name, i.website,
                     i.fee_description, i.notes, i.active
            ORDER BY i.name
            """
        )
        return cur.fetchall()


# ── CSV template download ─────────────────────────────────────

@router.get("/intermediaries/template")
def download_template():
    with get_db() as cur:
        cur.execute("SELECT cpt_code, short_description FROM cpt_codes ORDER BY cpt_code")
        cpt_rows = cur.fetchall()
        cur.execute("SELECT name FROM intermediaries WHERE active = TRUE ORDER BY name")
        intermediaries = [r["name"] for r in cur.fetchall()]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["intermediary_name", "payer_name", "cpt_code", "state",
                     "allowed_amount", "effective_date", "provider"])
    writer.writerow(["# provider column: JJ = Jodene Jensen, KR = Katherine Robins, "
                     "LK = Lori Kistler, blank = COMMON (all providers)", "", "", "", "", "", ""])

    key_cpts = ["99214", "99215", "90833", "90836", "90838",
                "99204", "99205", "90785", "98003", "98002", "98006", "98007"]
    cpt_lookup = {r["cpt_code"]: r for r in cpt_rows}
    example_cpts = [cpt_lookup[c] for c in key_cpts if c in cpt_lookup]

    for intermediary in intermediaries:
        for row in example_cpts:
            writer.writerow([intermediary, "", row["cpt_code"], "FL", "",
                             date.today().isoformat(), ""])
        writer.writerow([])

    output.seek(0)
    filename = f"intermediary_rates_template_{date.today()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── CSV import ────────────────────────────────────────────────

@router.post("/intermediaries/import")
async def import_rates(file: UploadFile = File(...),
                       intermediary_name: str = None,
                       state: str = Query(default="FL")):
    """
    Upload the SLAVE sheet intermediary_rates CSV (from populate_slave_v9.gs).

    Accepts the long-format CSV produced by the Apps Script:
      intermediary_name, payer_name, cpt_code, state, allowed_amount,
      effective_date, provider

    provider column: JJ / KR / LK / blank (blank = COMMON, applies to all providers)
    intermediary_name: Alma / Headway / Grow Therapy / SBH
    Safe to re-upload — uses INSERT ON CONFLICT DO UPDATE.
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    imported = 0
    skipped = 0
    errors = []

    with get_db() as cur:
        cur.execute("SELECT name, intermediary_id FROM intermediaries WHERE active = TRUE")
        intermediary_map = {r["name"].strip().lower(): r["intermediary_id"]
                            for r in cur.fetchall()}

        cur.execute("SELECT cpt_code FROM cpt_codes")
        valid_cpts = {r["cpt_code"] for r in cur.fetchall()}

        dict_reader = csv.DictReader(io.StringIO(text))

        for i, row in enumerate(dict_reader, start=2):
            # Skip comment/instruction rows
            first_val = (list(row.values())[0] or "").strip()
            if first_val.startswith("#"):
                skipped += 1
                continue

            iname = (row.get("intermediary_name") or "").strip()
            if not iname:
                skipped += 1
                continue

            intermediary_id = intermediary_map.get(iname.lower())
            if not intermediary_id:
                errors.append(f"Row {i}: unknown intermediary '{iname}' — skipped")
                skipped += 1
                continue

            pname = (row.get("payer_name") or "").strip() or None
            cpt_code = (row.get("cpt_code") or "").strip()
            if not cpt_code:
                skipped += 1
                continue

            # Auto-register unknown CPT codes
            if cpt_code not in valid_cpts:
                category = ("Telehealth E/M" if cpt_code.startswith("980") else
                            "Psychiatric" if cpt_code.startswith("908") else "E/M")
                cur.execute(
                    "INSERT INTO cpt_codes (cpt_code, short_description, category, "
                    "is_time_based, is_addon, primary_code_required, telehealth_eligible) "
                    "VALUES (%s, %s, %s, FALSE, FALSE, FALSE, TRUE) ON CONFLICT DO NOTHING",
                    (cpt_code, cpt_code, category),
                )
                valid_cpts.add(cpt_code)

            amount_raw = (row.get("allowed_amount") or "").strip().replace("$", "").replace(",", "")
            if not amount_raw:
                skipped += 1
                continue
            try:
                allowed_amount = float(amount_raw)
            except ValueError:
                errors.append(f"Row {i}: invalid amount '{amount_raw}' — skipped")
                skipped += 1
                continue

            # State: use row value if present, fall back to query param
            row_state = (row.get("state") or "").strip().upper()
            record_state = row_state if row_state else (state or "FL").upper()

            eff_raw = (row.get("effective_date") or "").strip()
            effective_date = eff_raw if eff_raw else None

            # Provider: JJ / KR / LK / None (COMMON)
            provider_raw = (row.get("provider") or "").strip().upper()
            provider = provider_raw if provider_raw in ("JJ", "KR", "LK") else None

            if pname:
                cur.execute(
                    "INSERT INTO intermediary_payer_map (intermediary_payer_name) "
                    "VALUES (%s) ON CONFLICT DO NOTHING",
                    (pname,),
                )

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
                    (intermediary_id, pname, cpt_code, record_state,
                     allowed_amount, effective_date, provider),
                )
                imported += 1
            except Exception as e:
                errors.append(f"Row {i}: DB error — {str(e)}")
                skipped += 1

    return {
        "status":   "ok",
        "imported": imported,
        "skipped":  skipped,
        "errors":   errors[:20],
        "message":  f"Imported {imported} rate(s). {skipped} row(s) skipped.",
    }


# ── Raw row lookup (added 2026-10-01) ──────────────────────────
# No existing endpoint lets you look at raw intermediary_rates rows by
# payer_name -- channel-comparison joins through payers/cpt_codes and
# won't surface an orphaned/unmapped payer_name like "OPTUM" at all, by
# design. Added for debugging and one-off cleanup scripts (see
# cleanup_orphaned_optum_rows.py), which need to find exactly which rows
# a payer_name typo created before they can be removed. Deliberately
# exact-match only on every filter, no substring/fuzzy search, so a
# caller can't accidentally pull back more than it asked for.

@router.get("/intermediaries/rows")
def list_rate_rows(
    payer_name: str = Query(default=None),
    intermediary_name: str = Query(default=None),
    state: str = Query(default=None),
):
    conditions = []
    params = []
    if payer_name:
        conditions.append("ir.payer_name = %s")
        params.append(payer_name)
    if intermediary_name:
        conditions.append("i.name = %s")
        params.append(intermediary_name)
    if state:
        conditions.append("ir.state = %s")
        params.append(state.upper())
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with get_db() as cur:
        cur.execute(
            f"""
            SELECT ir.rate_id, i.name AS intermediary_name, ir.payer_name,
                   ir.cpt_code, ir.state, ir.allowed_amount,
                   ir.effective_date, ir.provider, ir.updated_at
            FROM intermediary_rates ir
            JOIN intermediaries i ON ir.intermediary_id = i.intermediary_id
            {where}
            ORDER BY i.name, ir.cpt_code, ir.provider
            """,
            params,
        )
        return cur.fetchall()


# ── Explicit-key deletion (added 2026-09-30, Provider Rate Overrides) ──
# /api/intermediaries/import is an upsert (INSERT ... ON CONFLICT DO
# UPDATE) and never deletes -- there was no way to remove a row short of
# a manual SQL DELETE. Added for two related cleanup cases:
#   1. A group's first per-provider override leaves its old blank/COMMON
#      row (provider IS NULL) stale in Postgres -- its key never matches
#      the new per-provider rows, so the upsert never touches it. This is
#      the route merge_provider_overrides() calls automatically via
#      push_superseded_deletes() in sync_rates_from_sheets.py.
#   2. One-off cleanup of rows written under a wrong/orphaned payer_name
#      before payer_name validation existed -- see
#      cleanup_orphaned_optum_rows.py (2026-10-01 incident).
# Deliberately requires the FULL natural key per row (never a broad
# filter) so a caller has to know precisely what it's removing.

@router.post("/intermediaries/delete-superseded", response_model=DeleteSupersededResponse)
async def delete_superseded_rows(request: DeleteSupersededRequest):
    with get_db() as cur:
        cur.execute("SELECT name, intermediary_id FROM intermediaries WHERE active = TRUE")
        intermediary_map = {r["name"].strip().lower(): r["intermediary_id"]
                            for r in cur.fetchall()}

        deleted = 0
        not_found = 0
        errors = []
        for i, key in enumerate(request.keys, start=1):
            intermediary_id = intermediary_map.get(key.intermediary_name.strip().lower())
            if not intermediary_id:
                errors.append(f"Key {i}: unknown intermediary '{key.intermediary_name}'")
                continue
            if not key.payer_name or not key.cpt_code or not key.state:
                errors.append(f"Key {i}: missing payer_name/cpt_code/state — {key}")
                continue

            provider = (key.provider or "").strip().upper() or None

            cur.execute(
                """
                DELETE FROM intermediary_rates
                WHERE intermediary_id = %s
                  AND payer_name = %s
                  AND cpt_code = %s
                  AND state = %s
                  AND provider IS NOT DISTINCT FROM %s
                RETURNING rate_id
                """,
                (intermediary_id, key.payer_name.strip(), key.cpt_code.strip(),
                 key.state.strip().upper(), provider),
            )
            rows = cur.fetchall()
            if rows:
                deleted += 1
            else:
                not_found += 1

    return DeleteSupersededResponse(deleted=deleted, not_found=not_found, errors=errors)


# ── Channel Comparison ────────────────────────────────────────

VALID_STATES = {
    "AK", "AZ", "CO", "CT", "DC", "FL", "HI", "ID", "IA", "KS", "ME", "MD",
    "MN", "MT", "NE", "NV", "NH", "NM", "ND", "OR", "SD", "UT", "VT", "WA", "WY",
}

CHANNEL_COMPARISON_SQL = """
WITH
direct_rates AS (
    -- FIXED 2026-08-23: fee_schedule_lines has a real `state` column (added
    -- by sql/26_fee_schedule_state.sql specifically for this reason — that
    -- migration even tagged Regence Oregon's row `state = 'OR'` at the
    -- time) with real semantics: NULL = applies to every state, a specific
    -- value = applies only there. This inline copy of the query never
    -- picked up that migration's fix — only the separate `v_channel_
    -- comparison` SQL view did. Found via a real case: querying state=HI
    -- surfaced Regence BCBS Oregon (a payer with no Hawaii data at all)
    -- showing only a Medicare rate and "No Data" for best channel — the
    -- exact signature of a payer whose OR-tagged direct contract leaked
    -- into every other state's query. The tiebreak below (state-specific
    -- row wins over a universal one) matches the view's own ordering.
    SELECT DISTINCT ON (p.payer_name, fsl.cpt_code)
        p.payer_name,
        fsl.cpt_code,
        fsl.allowed_amount AS direct_rate
    FROM fee_schedule_lines fsl
    JOIN contracts         c   ON fsl.contract_id      = c.contract_id
    JOIN payers            p   ON c.payer_id           = p.payer_id
    JOIN provider_entities pe  ON c.provider_entity_id = pe.provider_entity_id
    WHERE c.active = TRUE
      AND (c.end_date   IS NULL OR c.end_date   >= CURRENT_DATE)
      AND (fsl.end_date IS NULL OR fsl.end_date >= CURRENT_DATE)
      AND (fsl.state IS NULL OR fsl.state = %(state)s)
    ORDER BY p.payer_name, fsl.cpt_code,
        CASE pe.entity_type WHEN 'NPI1' THEN 0 ELSE 1 END,
        CASE WHEN fsl.state IS NOT NULL THEN 0 ELSE 1 END,
        fsl.allowed_amount DESC
),
medicare AS (
    SELECT cpt_code, allowed_amount AS medicare_allowed
    FROM   benchmark_fee_schedule
    WHERE  source_name   = 'Medicare 2026'
      AND  effective_year = 2026
      AND  locality       = %(state)s
),
channel_cpts AS (
    SELECT unnest(ARRAY[
        '99214','99215','90833','90836','90838',
        '99204','99205','90785',
        '98002','98003','98006','98007'
    ]) AS cpt_code
),
all_combos AS (
    -- BUGFIX 2026-08: this branch previously had no state filter at all, so
    -- ANY (payer_name, cpt_code) combo that existed for ANY state — even one
    -- with zero intermediary_rates rows for the state actually being
    -- queried — became a candidate row here. Downstream, that ghost combo
    -- gets LEFT JOINed against the correctly state-scoped alma/headway/
    -- grow/sbh CTEs (which find nothing for it), but it still surfaces as a
    -- row in the API response — e.g. querying state=CO could return a row
    -- labeled "Anthem BCBS Maine" even though that payer has no CO data at
    -- all. Adding ir.state here scopes the candidate list to the state
    -- actually being requested, matching every other CTE below.
    SELECT DISTINCT ir.payer_name, ir.cpt_code
    FROM   intermediary_rates ir
    WHERE  ir.payer_name IS NOT NULL
      AND  ir.cpt_code IN (SELECT cpt_code FROM channel_cpts)
      AND  ir.state = %(state)s
      AND  (%(provider_filter)s IS NULL OR ir.provider IS NULL OR ir.provider = %(provider_filter)s)
    UNION
    -- FIXED 2026-08-23: same underlying issue as direct_rates above — this
    -- branch builds the candidate (payer, cpt) list from the same table,
    -- so it needs the identical NULL-or-matching-state treatment, not a
    -- plain state filter (which would wrongly exclude legitimate
    -- universal/NULL-state rates from every state's candidate list).
    SELECT DISTINCT p.payer_name, fsl.cpt_code
    FROM   fee_schedule_lines fsl
    JOIN   contracts c ON fsl.contract_id = c.contract_id
    JOIN   payers    p ON c.payer_id      = p.payer_id
    WHERE  c.active = TRUE
      AND (c.end_date   IS NULL OR c.end_date   >= CURRENT_DATE)
      AND (fsl.end_date IS NULL OR fsl.end_date >= CURRENT_DATE)
      AND  fsl.cpt_code IN (SELECT cpt_code FROM channel_cpts)
      AND (fsl.state IS NULL OR fsl.state = %(state)s)
),
headway AS (
    SELECT DISTINCT ON (ir.payer_name, ir.cpt_code)
        ir.payer_name, ir.cpt_code,
        ir.allowed_amount AS headway_rate,
        ir.updated_at     AS headway_updated_at
    FROM   intermediary_rates ir
    JOIN   intermediaries i ON ir.intermediary_id = i.intermediary_id
    WHERE  i.name = 'Headway' AND i.active = TRUE
      AND  (ir.effective_date IS NULL OR ir.effective_date <= CURRENT_DATE)
      AND  ir.state = %(state)s
      AND  (
            %(provider_filter)s IS NULL
            OR ir.provider IS NULL
            OR ir.provider = %(provider_filter)s
          )
      AND  NOT (%(provider_filter)s IS NOT NULL
                AND ir.provider IS NOT NULL
                AND ir.provider != %(provider_filter)s)
    ORDER BY ir.payer_name, ir.cpt_code, ir.updated_at DESC, ir.allowed_amount DESC
),
alma AS (
    SELECT DISTINCT ON (ir.payer_name, ir.cpt_code)
        ir.payer_name, ir.cpt_code,
        ir.allowed_amount AS alma_rate,
        ir.updated_at     AS alma_updated_at
    FROM   intermediary_rates ir
    JOIN   intermediaries i ON ir.intermediary_id = i.intermediary_id
    WHERE  i.name = 'Alma' AND i.active = TRUE
      AND  (ir.effective_date IS NULL OR ir.effective_date <= CURRENT_DATE)
      AND  ir.state = %(state)s
      AND  (
            %(provider_filter)s IS NULL
            OR ir.provider IS NULL
            OR ir.provider = %(provider_filter)s
          )
      AND  NOT (%(provider_filter)s IS NOT NULL
                AND ir.provider IS NOT NULL
                AND ir.provider != %(provider_filter)s)
    ORDER BY ir.payer_name, ir.cpt_code, ir.updated_at DESC, ir.allowed_amount DESC
),
grow AS (
    SELECT DISTINCT ON (ir.payer_name, ir.cpt_code)
        ir.payer_name, ir.cpt_code,
        ir.allowed_amount AS grow_rate,
        ir.updated_at     AS grow_updated_at
    FROM   intermediary_rates ir
    JOIN   intermediaries i ON ir.intermediary_id = i.intermediary_id
    WHERE  i.name = 'Grow Therapy' AND i.active = TRUE
      AND  (ir.effective_date IS NULL OR ir.effective_date <= CURRENT_DATE)
      AND  ir.state = %(state)s
      AND  (
            %(provider_filter)s IS NULL
            OR ir.provider IS NULL
            OR ir.provider = %(provider_filter)s
          )
      AND  NOT (%(provider_filter)s IS NOT NULL
                AND ir.provider IS NOT NULL
                AND ir.provider != %(provider_filter)s)
    ORDER BY ir.payer_name, ir.cpt_code, ir.updated_at DESC, ir.allowed_amount DESC
),
sbh AS (
    SELECT DISTINCT ON (ir.payer_name, ir.cpt_code)
        ir.payer_name, ir.cpt_code,
        ir.allowed_amount AS direct_rate,
        ir.updated_at     AS direct_updated_at
    FROM   intermediary_rates ir
    JOIN   intermediaries i ON ir.intermediary_id = i.intermediary_id
    WHERE  i.name = 'SBH' AND i.active = TRUE
      AND  (ir.effective_date IS NULL OR ir.effective_date <= CURRENT_DATE)
      AND  ir.state = %(state)s
      AND  (
            -- No provider filter: show all SBH rates
            %(provider_filter)s IS NULL
            OR
            -- Provider filter active: only show rates tagged for that provider
            -- OR truly COMMON (NULL) rates — NOT rates tagged for other providers
            (ir.provider IS NULL OR ir.provider = %(provider_filter)s)
            AND NOT (ir.provider IS NOT NULL AND ir.provider != %(provider_filter)s)
          )
    ORDER BY ir.payer_name, ir.cpt_code, ir.updated_at DESC, ir.allowed_amount DESC
),
combined AS (
    SELECT
        p.payer_id,
        ac.payer_name,
        ac.cpt_code,
        cc.short_description,
        cc.category,
        m.medicare_allowed,
        s.direct_rate AS direct_rate,
        CASE WHEN s.direct_rate IS NOT NULL AND m.medicare_allowed > 0
             THEN ROUND((s.direct_rate / m.medicare_allowed * 100)::numeric, 1)
        END AS direct_pct_of_medicare,
        h.headway_rate,    h.headway_updated_at,
        a.alma_rate,       a.alma_updated_at,
        g.grow_rate,       g.grow_updated_at,
        LEAST(h.headway_updated_at, a.alma_updated_at, g.grow_updated_at)
            AS oldest_intermediary_update,
        CASE
            WHEN GREATEST(
                COALESCE(s.direct_rate, COALESCE(dr.direct_rate, 0)),
                COALESCE(h.headway_rate, 0),
                COALESCE(a.alma_rate,    0),
                COALESCE(g.grow_rate,    0)
            ) = 0 THEN 'No Data'
            WHEN COALESCE(s.direct_rate, 0) >= COALESCE(h.headway_rate, 0)
             AND COALESCE(s.direct_rate, 0) >= COALESCE(a.alma_rate, 0)
             AND COALESCE(s.direct_rate, 0) >= COALESCE(g.grow_rate, 0)
             AND s.direct_rate IS NOT NULL
            THEN 'Direct'
            ELSE 'Intermediary'
        END AS best_channel_type,
        CASE WHEN s.direct_rate IS NOT NULL THEN TRUE ELSE FALSE END AS has_direct_contract
    FROM  all_combos ac
    JOIN  cpt_codes       cc  ON cc.cpt_code  = ac.cpt_code
    LEFT JOIN medicare    m   ON m.cpt_code   = ac.cpt_code
    LEFT JOIN payers      p   ON lower(p.payer_name) = lower(ac.payer_name)
    LEFT JOIN direct_rates dr ON lower(dr.payer_name) = lower(ac.payer_name)
                              AND dr.cpt_code  = ac.cpt_code
    LEFT JOIN sbh         s   ON s.payer_name  = ac.payer_name
                              AND s.cpt_code   = ac.cpt_code
    LEFT JOIN headway     h   ON h.payer_name  = ac.payer_name AND h.cpt_code = ac.cpt_code
    LEFT JOIN alma        a   ON a.payer_name  = ac.payer_name AND a.cpt_code = ac.cpt_code
    LEFT JOIN grow        g   ON g.payer_name  = ac.payer_name AND g.cpt_code = ac.cpt_code
)
SELECT DISTINCT ON (payer_name, cpt_code)
    payer_id, payer_name, cpt_code, short_description, category,
    medicare_allowed, direct_rate, direct_pct_of_medicare,
    headway_rate, headway_updated_at,
    alma_rate,    alma_updated_at,
    grow_rate,    grow_updated_at,
    oldest_intermediary_update,
    best_channel_type, has_direct_contract
FROM combined
ORDER BY payer_name, cpt_code
"""


@router.get("/channel-comparison")
def get_channel_comparison(
    payer_id:   int = None,
    payer_name: str = None,
    cpt_code:   str = None,
    best_only:  bool = False,
    provider:   str = Query(default=None, description="JJ, KR, or LK"),
    state:      str = Query(default="FL"),
):
    state_upper = (state or "FL").upper()
    if state_upper not in VALID_STATES:
        state_upper = "FL"

    provider_filter = provider.upper() if provider and provider.upper() in ("JJ","KR","LK") else None

    conditions = []
    params: dict = {"state": state_upper, "provider_filter": provider_filter}
    if payer_id:
        conditions.append("payer_id = %(payer_id)s")
        params["payer_id"] = payer_id
    if payer_name:
        conditions.append("payer_name = %(payer_name)s")
        params["payer_name"] = payer_name
    if cpt_code:
        conditions.append("cpt_code = %(cpt_code)s")
        params["cpt_code"] = cpt_code
    if best_only:
        conditions.append("best_channel_type = 'Intermediary'")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM ({CHANNEL_COMPARISON_SQL}) AS ch {where}"

    with get_db() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@router.get("/channel-comparison/summary")
def get_channel_comparison_summary():
    with get_db() as cur:
        cur.execute(
            """
            SELECT payer_id, payer_name,
                COUNT(*) FILTER (WHERE best_channel_type = 'Direct')       AS direct_best_count,
                COUNT(*) FILTER (WHERE best_channel_type = 'Intermediary') AS intermediary_best_count,
                COUNT(*)                                                    AS total_codes,
                ROUND(AVG(direct_rate)::numeric, 2)                        AS avg_direct_rate,
                ROUND(AVG(GREATEST(
                    COALESCE(headway_rate, 0),
                    COALESCE(alma_rate, 0),
                    COALESCE(grow_rate, 0)
                ))::numeric, 2)                                             AS avg_best_intermediary_rate
            FROM v_channel_comparison
            WHERE payer_id IS NOT NULL
            GROUP BY payer_id, payer_name
            ORDER BY intermediary_best_count DESC, payer_name
            """
        )
        return cur.fetchall()


@router.get("/channel-comparison/export")
def export_channel_comparison(payer_id: int = None, state: str = Query(default="FL")):
    where = "WHERE payer_id = %s" if payer_id else ""
    params = [payer_id] if payer_id else []
    with get_db() as cur:
        cur.execute(f"SELECT * FROM v_channel_comparison {where}", params)
        rows = cur.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="No channel comparison data found.")
    columns = ["payer_id","payer_name","cpt_code","short_description","category",
               "modifier","medicare_allowed","direct_pct_of_medicare",
               "direct_rate","headway_rate","alma_rate","grow_rate","best_channel_type"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in columns})
    output.seek(0)
    filename = f"channel_comparison_export_{date.today()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/billing-actuals")
def get_billing_actuals(
    state: str = Query(default="FL"),
    primary_cpt: str = Query(default=None),
    addon_cpt: str = Query(default=None),
    intermediary: str = Query(default=None),
    provider_name: str = Query(default=None),
):
    state_upper = (state or "FL").upper()
    conditions = ["state = %(state)s"]
    params: dict = {"state": state_upper}
    if primary_cpt:
        conditions.append("primary_cpt = %(primary_cpt)s")
        params["primary_cpt"] = primary_cpt.strip()
    if addon_cpt:
        if addon_cpt.lower() == "none":
            conditions.append("addon_cpt IS NULL")
        else:
            conditions.append("addon_cpt = %(addon_cpt)s")
            params["addon_cpt"] = addon_cpt.strip()
    if intermediary:
        conditions.append("intermediary = %(intermediary)s")
        params["intermediary"] = intermediary.strip()
    if provider_name and provider_name.lower() not in ("all", "all providers", ""):
        conditions.append("provider_name = %(provider_name)s")
        params["provider_name"] = provider_name.strip()
    where = "WHERE " + " AND ".join(conditions)
    with get_db() as cur:
        cur.execute(
            f"""SELECT intermediary, provider_name, insurance_plan, state,
                primary_cpt, addon_cpt, session_type, avg_payment, session_count,
                min_payment, max_payment, effective_year, updated_at
                FROM v_billing_actuals {where}
                ORDER BY insurance_plan, session_type, intermediary""",
            params,
        )
        return cur.fetchall()
