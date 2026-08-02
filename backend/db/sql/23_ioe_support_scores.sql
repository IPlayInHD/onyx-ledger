-- =============================================================================
-- Onyx Ledger — 23 · IOE support-score persistence  (schema: ioe)
-- Alembic revision: 0029_ioe_support_scores
--
-- Independently reviewable: this file does ONE thing — persist the five-stage
-- support-score model agreed in the P2 verification record. It has no dependency
-- on the rest of P3 and can be reviewed, applied, or reverted on its own.
--
-- P1 stored a single `confidence_score smallint`. The revised model keeps three
-- distinct numbers plus the cap decision, because each answers a different
-- question and collapsing them changes the meaning of the score:
--
--   raw_support_score          support before any assumption adjustment
--   assumption_adjusted_score  after uncertainty from materiality, source,
--                              evidence, eligibility impact, and measured
--                              sensitivity — UNCAPPED, and the value used as the
--                              secondary ordering key
--   display_support_score      min(assumption_adjusted_score, 80) — user-facing
--   support_cap_applied        whether the cap actually bound
--   support_cap_reason_code    why, when it did
--
-- TERMINOLOGY: this is a SUPPORT / RELIABILITY score. It is not a probability of
-- CRA acceptance, nor a probability of receiving the displayed amount. API field
-- names use `support` accordingly.
--
-- ALL ADDITIVE. No column is dropped or retyped; existing rows keep their values.
-- =============================================================================

ALTER TABLE ioe.optimization_candidate
    ADD COLUMN raw_support_score         numeric(5,2),
    ADD COLUMN assumption_adjusted_score numeric(5,2),
    ADD COLUMN display_support_score     numeric(5,2),
    ADD COLUMN support_cap_applied       boolean NOT NULL DEFAULT false,
    ADD COLUMN support_cap_reason_code   text;

-- Range checks added NOT VALID then validated separately, so the ACCESS
-- EXCLUSIVE lock is not held across the scan.
ALTER TABLE ioe.optimization_candidate
    ADD CONSTRAINT candidate_raw_support_range
        CHECK (raw_support_score IS NULL OR raw_support_score BETWEEN 0 AND 100) NOT VALID,
    ADD CONSTRAINT candidate_adjusted_support_range
        CHECK (assumption_adjusted_score IS NULL
               OR assumption_adjusted_score BETWEEN 0 AND 100) NOT VALID,
    ADD CONSTRAINT candidate_display_support_range
        CHECK (display_support_score IS NULL
               OR display_support_score BETWEEN 0 AND 100) NOT VALID;

-- The display value is a CAP of the adjusted value, never an independent number.
ALTER TABLE ioe.optimization_candidate
    ADD CONSTRAINT candidate_display_not_above_adjusted
        CHECK (display_support_score IS NULL
               OR assumption_adjusted_score IS NULL
               OR display_support_score <= assumption_adjusted_score) NOT VALID;

-- A cap reason exists exactly when the cap was applied.
ALTER TABLE ioe.optimization_candidate
    ADD CONSTRAINT candidate_cap_reason_iff_capped
        CHECK ((support_cap_applied AND support_cap_reason_code IS NOT NULL)
               OR (NOT support_cap_applied AND support_cap_reason_code IS NULL)) NOT VALID;

ALTER TABLE ioe.optimization_candidate VALIDATE CONSTRAINT candidate_raw_support_range;
ALTER TABLE ioe.optimization_candidate VALIDATE CONSTRAINT candidate_adjusted_support_range;
ALTER TABLE ioe.optimization_candidate VALIDATE CONSTRAINT candidate_display_support_range;
ALTER TABLE ioe.optimization_candidate VALIDATE CONSTRAINT candidate_display_not_above_adjusted;
ALTER TABLE ioe.optimization_candidate VALIDATE CONSTRAINT candidate_cap_reason_iff_capped;

-- =============================================================================
-- Handling of the pre-existing `confidence_score` column
--
-- DECISION: RETAINED for additive compatibility, and REDEFINED as the persisted
-- integer equivalent of `display_support_score`. It is no longer an independent
-- input — any value supplied by a caller is overwritten.
--
-- Divergence is prevented STRUCTURALLY, not by convention:
--   1. a BEFORE INSERT/UPDATE trigger DERIVES it from display_support_score, so
--      an application cannot write a conflicting value even by mistake;
--   2. a CHECK constraint then asserts the two agree, so a direct SQL write that
--      somehow bypassed the derivation is still rejected.
--
-- Rounding is half-up to a whole number, matching the ROUND_HALF_UP money/score
-- policy used throughout the domain. (PostgreSQL's numeric round() is half-up
-- away from zero; support scores are non-negative, so the two agree.)
-- =============================================================================
CREATE OR REPLACE FUNCTION ioe.derive_confidence_score()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.display_support_score IS NOT NULL THEN
        NEW.confidence_score := round(NEW.display_support_score)::smallint;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_derive_confidence_score
    BEFORE INSERT OR UPDATE ON ioe.optimization_candidate
    FOR EACH ROW EXECUTE FUNCTION ioe.derive_confidence_score();

ALTER TABLE ioe.optimization_candidate
    ADD CONSTRAINT candidate_confidence_matches_display
        CHECK (display_support_score IS NULL
               OR confidence_score IS NULL
               OR confidence_score = round(display_support_score)::smallint) NOT VALID;
ALTER TABLE ioe.optimization_candidate
    VALIDATE CONSTRAINT candidate_confidence_matches_display;

COMMENT ON COLUMN ioe.optimization_candidate.confidence_score IS
    'DERIVED from display_support_score by trigger; never supplied by callers. A support score, not a probability.';
COMMENT ON COLUMN ioe.optimization_candidate.assumption_adjusted_score IS
    'Uncapped support after assumption uncertainty; secondary ordering key.';
