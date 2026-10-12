"""
routers/comprehensive_rates.py
-------------------------------
Shared server-side storage for the dashboard's "Comprehensive Contract
Rate Reference" section (manual payer x CPT code contracted-rate lookup).

Previously this data lived only in each browser's localStorage, so it was
invisible from any other computer on the LAN. This router moves it into
Postgres (table comprehensive_rates, sql/34_comprehensive_rates.sql).

Deliberately independent of intermediary_rates, fee_schedule_lines and the
SolBoard best-channel pipeline. The ONLY thing that writes here is the
section's own CSV upload (POST) and its Clear Rates button (DELETE).

CSV parsing and validation still happen in the browser (flexible column
names, skip blank/invalid rows); this router receives already-parsed rows.
"""

from fastapi import APIRouter
from ..database import get_db
from ..models import (
    ComprehensiveRate,
    ComprehensiveRatesUploadRequest,
    ComprehensiveRatesUploadResponse,
)

router = APIRouter(prefix="/api", tags=["Comprehensive Rate Reference"])


@router.get("/comprehensive-rates", response_model=list[ComprehensiveRate])
def list_comprehensive_rates():
    """Return every stored comprehensive rate row."""
    with get_db() as cur:
        cur.execute(
            """
            SELECT rate_id, payer, cpt_code, allowed_amount,
                   effective_date, updated_at
            FROM comprehensive_rates
            ORDER BY payer, cpt_code
            """
        )
        return cur.fetchall()


@router.post("/comprehensive-rates", response_model=ComprehensiveRatesUploadResponse)
def upload_comprehensive_rates(payload: ComprehensiveRatesUploadRequest):
    """
    Upsert a batch of (payer, cpt_code) rate rows.
    Safe to re-upload: matches on the (payer, cpt_code) unique constraint,
    so an existing row is updated rather than duplicated. Rows not in the
    upload are left alone.
    """
    upserted = 0
    with get_db() as cur:
        for row in payload.rows:
            cur.execute(
                """
                INSERT INTO comprehensive_rates
                    (payer, cpt_code, allowed_amount, effective_date, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT ON CONSTRAINT comprehensive_rates_unique
                DO UPDATE SET
                    allowed_amount = EXCLUDED.allowed_amount,
                    effective_date = EXCLUDED.effective_date,
                    updated_at     = NOW()
                """,
                (row.payer.strip(), row.cpt_code.strip().upper(),
                 row.allowed_amount, row.effective_date),
            )
            upserted += 1
    return ComprehensiveRatesUploadResponse(
        upserted=upserted,
        message=f"Upserted {upserted} rate(s).",
    )


@router.delete("/comprehensive-rates")
def clear_comprehensive_rates():
    """Delete every stored comprehensive rate row (the Clear Rates button)."""
    with get_db() as cur:
        cur.execute("DELETE FROM comprehensive_rates RETURNING rate_id")
        deleted = len(cur.fetchall())
    return {"deleted": deleted, "message": f"Cleared {deleted} rate(s)."}
