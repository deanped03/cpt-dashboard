"""
models.py
---------
Pydantic models for request validation and response serialization.
These match the shapes defined in the api-examples JSON files.
"""

from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel


# ──────────────────────────────────────────────
# Payers
# ──────────────────────────────────────────────

class Payer(BaseModel):
    payer_id: int
    payer_name: str
    payer_display_name: Optional[str]
    payer_type: Optional[str]
    payer_notes: Optional[str]


# ──────────────────────────────────────────────
# Provider Entities
# ──────────────────────────────────────────────

class ProviderEntity(BaseModel):
    provider_entity_id: int
    legal_name: str
    npi_number: str
    entity_type: str
    tax_id: Optional[str]
    active: bool
    notes: Optional[str]


# ──────────────────────────────────────────────
# CPT Codes
# ──────────────────────────────────────────────

class CptCode(BaseModel):
    cpt_code: str
    short_description: str
    category: str
    typical_time_minutes: Optional[int]
    is_time_based: bool
    is_addon: bool
    primary_code_required: bool
    primary_code_family: Optional[str]
    telehealth_eligible: bool
    typical_use: Optional[str]
    notes: Optional[str]


# ──────────────────────────────────────────────
# Contracts
# ──────────────────────────────────────────────

class Contract(BaseModel):
    contract_id: int
    payer_id: int
    payer_name: str
    provider_entity_id: int
    provider_name: str
    npi_number: str
    entity_type: str
    payer_contract_id: Optional[str]
    product_line: Optional[str]
    line_of_business: Optional[str]
    effective_date: Optional[date]
    end_date: Optional[date]
    active: bool
    notes: Optional[str]


# ──────────────────────────────────────────────
# Fee Schedule Import
# ──────────────────────────────────────────────

class FeeScheduleLineIn(BaseModel):
    cpt_code: str
    modifier: Optional[str] = None
    place_of_service: Optional[str] = None
    unit_type: str = "per_service"
    allowed_amount: Decimal
    effective_date: Optional[date] = None
    end_date: Optional[date] = None
    notes: Optional[str] = None


class FeeScheduleImportRequest(BaseModel):
    contract_id: int
    lines: list[FeeScheduleLineIn]


class FeeScheduleImportResponse(BaseModel):
    contract_id: int
    lines_upserted: int
    message: str


# ──────────────────────────────────────────────
# Benchmark Rates
# ──────────────────────────────────────────────

class BenchmarkRateIn(BaseModel):
    cpt_code: str
    allowed_amount: Decimal
    notes: Optional[str] = None


class BenchmarkImportRequest(BaseModel):
    source_name: str          # e.g. "Medicare 2026"
    locality: str             # e.g. "FL"
    effective_year: int       # e.g. 2026
    rates: list[BenchmarkRateIn]


class BenchmarkImportResponse(BaseModel):
    source_name: str
    lines_upserted: int
    message: str


# ──────────────────────────────────────────────
# Superseded intermediary_rates row deletion (2026-09-30)
# Added for the Provider Rate Overrides merge: /intermediaries/import is a
# pure upsert and never deletes, so a combo's old blank/COMMON row has to
# be removed explicitly once an override replaces it with per-provider
# rows. Explicit full-key list only -- never a broad/pattern delete.
# ──────────────────────────────────────────────

class SupersededRateKey(BaseModel):
    intermediary_name: str
    payer_name: str
    cpt_code: str
    state: str
    provider: Optional[str] = None   # None/omitted = the COMMON row (provider IS NULL)


class DeleteSupersededRequest(BaseModel):
    keys: list[SupersededRateKey]


class DeleteSupersededResponse(BaseModel):
    deleted: int
    not_found: int
    errors: list[str]


# ──────────────────────────────────────────────
# Negotiation Dashboard
# ──────────────────────────────────────────────

class DashboardRow(BaseModel):
    contract_id: int
    payer_name: str
    provider_name: str
    npi_number: str
    entity_type: str
    payer_contract_id: Optional[str]
    product_line: Optional[str]
    cpt_code: str
    short_description: str
    category: str
    modifier: Optional[str]
    place_of_service: Optional[str]
    payer_allowed: Optional[Decimal]
    medicare_allowed: Optional[Decimal]
    pct_of_medicare: Optional[Decimal]
    target_pct_of_medicare: Optional[Decimal]
    target_allowed: Optional[Decimal]
    rate_gap_per_unit: Optional[Decimal]
    is_underpaid: Optional[bool]
    annual_volume: Optional[int]
    volume_year: Optional[int]
    annual_revenue_current: Optional[Decimal]
    annual_revenue_at_target: Optional[Decimal]
    annual_revenue_gap: Optional[Decimal]


class DashboardSummaryRow(BaseModel):
    payer_id: int
    payer_name: str
    codes_with_rates: int
    codes_underpaid: Optional[int]
    avg_pct_of_medicare: Optional[Decimal]
    avg_target_pct: Optional[Decimal]
    total_revenue_current: Optional[Decimal]
    total_revenue_at_target: Optional[Decimal]
    total_revenue_gap: Optional[Decimal]


# ──────────────────────────────────────────────
# Negotiation Targets
# ──────────────────────────────────────────────

class NegotiationTargetIn(BaseModel):
    payer_id: Optional[int] = None      # NULL = global default
    cpt_code: Optional[str] = None      # NULL = all codes for this payer
    target_pct_of_medicare: Decimal
    notes: Optional[str] = None


class NegotiationTarget(BaseModel):
    target_id: int
    payer_id: Optional[int]
    cpt_code: Optional[str]
    target_pct_of_medicare: Decimal
    notes: Optional[str]


# ──────────────────────────────────────────────
# Claims Volume
# ──────────────────────────────────────────────

class ClaimsVolumeIn(BaseModel):
    contract_id: int
    cpt_code: str
    modifier: Optional[str] = None
    calendar_year: int
    annual_volume: int
    notes: Optional[str] = None


class ClaimsVolume(BaseModel):
    volume_id: int
    contract_id: int
    cpt_code: str
    modifier: Optional[str]
    calendar_year: int
    annual_volume: int
    notes: Optional[str]
