-- =============================================================
-- 33_add_interactive_complexity_code.sql
-- Fixes a silent data-loss bug: CPT 90785 (Interactive Complexity
-- add-on) is hardcoded into the "core 12" negotiation whitelist in
-- backend/routers/dashboard.py and in every v_negotiation_dashboard
-- / v_sbh_vs_medicare rebuild since sql/14_fix_views.sql, but it was
-- never inserted into the cpt_codes master table.
-- =============================================================

BEGIN;

INSERT INTO cpt_codes (
    cpt_code, short_description, category, typical_time_minutes,
    is_time_based, is_addon, primary_code_required, primary_code_family,
    telehealth_eligible, typical_use, notes
) VALUES
    ('90785', 'Interactive complexity add-on', 'Add-on Psychotherapy', NULL,
     FALSE, TRUE, TRUE, '90791, 90792, 90832-90838, 90853',
     TRUE, 'interactive complexity', 'List separately in addition to the primary procedure code')
ON CONFLICT (cpt_code) DO NOTHING;

COMMIT;

SELECT cpt_code, short_description FROM cpt_codes WHERE cpt_code = '90785';
