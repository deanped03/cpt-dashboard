"""
routers/dashboard.py
--------------------
Endpoints for the negotiation dashboard and targets.
These are the heart of the tool — they answer:
  - Which codes and payers are underpaying us?
  - How much money are we leaving on the table?
  - What should we ask for in negotiations?
"""

import csv
import io
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from backend.database import get_db
from ..models import (
    DashboardRow,
    DashboardSummaryRow,
    NegotiationTargetIn,
    NegotiationTarget,
)

router = APIRouter(prefix="/api", tags=["Negotiation Dashboard"])

# Valid state codes — the 23 states where Solrei has licensed providers
VALID_STATES = {
    "AK","AZ","CO","CT","DC","FL","HI","ID","IA","KS","ME","MD",
    "MN","MT","NE","NV","NH","NM","ND","OR","SD","UT","VT","WA","WY",
}


def _set_locality(cur, state: str):
    """Set the benchmark locality session variable for multi-state view support."""
    state_upper = state.upper() if state else "FL"
    if state_upper not in VALID_STATES:
        state_upper = "FL"
    cur.execute("SET app.benchmark_locality = %s", (state_upper,))
    return state_upper


# ── Full Dashboard ────────────────────────────────────────────

@router.get("/dashboard")
def get_dashboard(
    payer_id: int = None,
    underpaid_only: bool = False,
    min_gap: float = None,
    state: str = Query(default="FL", description="Two-letter state code (e.g. FL, AZ, WA)"),
):
    """
    Return the full negotiation dashboard from v_negotiation_dashboard.

    Optional filters:
      - payer_id: filter to a specific payer
      - underpaid_only: if true, return only codes where payer rate < target
      - min_gap: only return rows where rate_gap_per_unit >= this value
      - state: two-letter state code for Medicare benchmark locality (default: FL)
    """
    filters = []
    params = []

    if payer_id:
        filters.append("payer_id = %s")
        params.append(payer_id)
    if underpaid_only:
        filters.append("is_underpaid = TRUE")
    if min_gap is not None:
        filters.append("rate_gap_per_unit >= %s")
        params.append(min_gap)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with get_db() as cur:
        _set_locality(cur, state)
        cur.execute(f"SELECT * FROM v_negotiation_dashboard {where}", params)
        return cur.fetchall()


# ── Payer Summary ─────────────────────────────────────────────

@router.get("/dashboard/summary")
def get_dashboard_summary(
    payer_id: int = None,
    state: str = Query(default="FL", description="Two-letter state code for Medicare benchmark"),
):
    """
    Return payer-level summary from v_negotiation_summary.
    Sorted by total revenue gap (biggest opportunity first).
    Answers: 'Which payer should we call first?'
    """
    where = "WHERE payer_id = %s" if payer_id else ""
    params = [payer_id] if payer_id else []

    with get_db() as cur:
        _set_locality(cur, state)
        cur.execute(f"SELECT * FROM v_negotiation_summary {where}", params)
        return cur.fetchall()


# ── Underpaid Codes for a Payer ───────────────────────────────

@router.get("/dashboard/underpaid/{payer_id}")
def get_underpaid_codes(payer_id: int):
    """
    Return all underpaid codes for a specific payer, sorted by revenue gap.
    This is the negotiation hit list for a single payer conversation.
    """
    with get_db() as cur:
        # Verify payer exists
        cur.execute(
            "SELECT payer_name FROM payers WHERE payer_id = %s", (payer_id,))
        payer = cur.fetchone()
        if not payer:
            raise HTTPException(
                status_code=404, detail=f"Payer {payer_id} not found")

        cur.execute(
            """
            SELECT DISTINCT ON (cpt_code)
                cpt_code,
                short_description,
                category,
                payer_allowed,
                medicare_allowed,
                pct_of_medicare,
                target_pct_of_medicare,
                target_allowed,
                rate_gap_per_unit,
                annual_volume,
                annual_revenue_current,
                annual_revenue_at_target,
                annual_revenue_gap
            FROM v_negotiation_dashboard
            WHERE payer_id = %s
              AND is_underpaid = TRUE
              AND cpt_code IN (
                  '99214','99215','90833','90836','90838',
                  '99204','99205','90785',
                  '98002','98003','98006','98007'
              )
            ORDER BY cpt_code, annual_revenue_gap DESC NULLS LAST
            """,
            (payer_id,),
        )
        rows = cur.fetchall()

    return {
        "payer_id": payer_id,
        "payer_name": payer["payer_name"],
        "underpaid_codes": rows,
        "total_underpaid_codes": len(rows),
    }


# ── Negotiation Targets ───────────────────────────────────────

@router.get("/targets", response_model=list[NegotiationTarget])
def list_targets():
    """Return all negotiation targets (global, payer-level, and code-specific)."""
    with get_db() as cur:
        cur.execute(
            """
            SELECT
                nt.*,
                p.payer_name
            FROM negotiation_targets nt
            LEFT JOIN payers p ON nt.payer_id = p.payer_id
            ORDER BY nt.payer_id NULLS FIRST, nt.cpt_code NULLS FIRST
            """
        )
        return cur.fetchall()


@router.post("/targets", response_model=NegotiationTarget)
def upsert_target(payload: NegotiationTargetIn):
    """
    Create or update a negotiation target.

    Priority system:
      - payer_id=null, cpt_code=null  → global default (applies to everything)
      - payer_id=X,    cpt_code=null  → payer-level default (applies to all codes for that payer)
      - payer_id=X,    cpt_code='Y'   → specific code for specific payer (highest priority)

    Example — set global default to 130% of Medicare:
    { "target_pct_of_medicare": 130.0, "notes": "Global floor" }

    Example — set Florida Blue 99214 target to 145%:
    { "payer_id": 1, "cpt_code": "99214", "target_pct_of_medicare": 145.0 }
    """
    with get_db() as cur:
        cur.execute(
            """
            INSERT INTO negotiation_targets (payer_id, cpt_code, target_pct_of_medicare, notes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (payer_id, cpt_code)
            DO UPDATE SET
                target_pct_of_medicare = EXCLUDED.target_pct_of_medicare,
                notes                  = EXCLUDED.notes,
                updated_at             = NOW()
            RETURNING *
            """,
            (payload.payer_id, payload.cpt_code,
             payload.target_pct_of_medicare, payload.notes),
        )
        return cur.fetchone()


@router.delete("/targets/{target_id}")
def delete_target(target_id: int):
    """Delete a negotiation target by ID."""
    with get_db() as cur:
        cur.execute(
            "DELETE FROM negotiation_targets WHERE target_id = %s RETURNING target_id",
            (target_id,),
        )
        deleted = cur.fetchone()
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Target {target_id} not found")
    return {"message": f"Target {target_id} deleted successfully"}


# ── CSV Export ────────────────────────────────────────────────

@router.get("/dashboard/export")
def export_dashboard_csv(
    payer_id: int = None,
    underpaid_only: bool = False,
    state: str = Query(default="FL"),
):
    """
    Export the full negotiation dashboard as a CSV file.
    Optional filters: payer_id, underpaid_only.
    """
    filters = []
    params = []
    if payer_id:
        filters.append("payer_id = %s")
        params.append(payer_id)
    if underpaid_only:
        filters.append("is_underpaid = TRUE")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with get_db() as cur:
        _set_locality(cur, state)
        cur.execute(f"SELECT * FROM v_negotiation_dashboard {where}", params)
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(
            status_code=404, detail="No data found for export.")

    output = io.StringIO()
    columns = [
        "payer_name", "provider_name", "npi_number", "entity_type",
        "payer_contract_id", "product_line", "cpt_code", "short_description",
        "category", "modifier", "payer_allowed", "medicare_allowed",
        "pct_of_medicare", "target_pct_of_medicare", "target_allowed",
        "rate_gap_per_unit", "is_underpaid", "annual_volume", "volume_year",
        "annual_revenue_current", "annual_revenue_at_target", "annual_revenue_gap",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k) for k in columns})

    output.seek(0)
    filename = f"negotiation_dashboard_export.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
