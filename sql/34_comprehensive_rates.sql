-- =============================================================
-- 34_comprehensive_rates.sql
-- Server-side storage for the dashboard's "Comprehensive Contract Rate
-- Reference — All CPT Codes by Payer" section.
--
-- Until now that section kept its data only in each browser's
-- localStorage (key solrei_comp_rates_v1), so rates uploaded on one
-- computer were invisible from every other computer on the LAN. This
-- table makes the data shared.
--
-- Deliberately independent of intermediary_rates / fee_schedule_lines /
-- the SolBoard best-channel pipeline: it holds only what is explicitly
-- uploaded through that section's CSV upload, nothing else writes to it.
-- payer is plain text (no FK to payers), matching how the frontend has
-- always stored it; the six allowed values are enforced client-side.
-- =============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS comprehensive_rates (
    rate_id         SERIAL PRIMARY KEY,
    payer           VARCHAR(100)  NOT NULL,
    cpt_code        VARCHAR(10)   NOT NULL,
    allowed_amount  NUMERIC(10,2) NOT NULL,
    effective_date  VARCHAR(20),
    updated_at      TIMESTAMP     NOT NULL DEFAULT NOW(),
    CONSTRAINT comprehensive_rates_unique UNIQUE (payer, cpt_code)
);

COMMIT;

SELECT 'comprehensive_rates' AS table_name, COUNT(*) AS row_count FROM comprehensive_rates;
