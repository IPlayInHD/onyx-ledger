-- =============================================================================
-- Onyx Ledger — 22 · Link reco.recommendation to the IOE  (schema: reco)
-- Alembic revision: 0028_reco_ioe_link
-- IOE architecture Revision 2 §7 + Revision 2.1 §F.
--
-- Deliberately AFTER 21_ioe.sql: the optimization_run_id foreign key requires
-- ioe.optimization_run to exist. Revision 2 had these changes bundled with the
-- rules-contract migration, which could not have applied.
--
-- ALL ADDITIVE. Existing rows keep their values; nothing is rewritten. The
-- lifecycle CHECK is widened to a SUPERSET so legacy values remain valid and no
-- backfill is required (expand-then-migrate).
-- =============================================================================

-- ---- Traceability + honest labelling on the canonical recommendation --------
ALTER TABLE reco.recommendation
    ADD COLUMN optimization_run_id uuid,
    ADD COLUMN calculation_basis   text,
    ADD COLUMN evidence_status     text;

-- FK added NOT VALID, then validated separately, so the ACCESS EXCLUSIVE lock is
-- not held while the table is scanned.
ALTER TABLE reco.recommendation
    ADD CONSTRAINT fk_recommendation_optimization_run
    FOREIGN KEY (optimization_run_id) REFERENCES ioe.optimization_run(id)
    ON DELETE SET NULL NOT VALID;
ALTER TABLE reco.recommendation VALIDATE CONSTRAINT fk_recommendation_optimization_run;

CREATE INDEX ix_reco_optimization_run ON reco.recommendation (optimization_run_id);

-- `calculation_basis` records HOW the figure was produced; `evidence_status`
-- records how well the INPUTS are supported. They are independent axes and are
-- never collapsed — a precisely-calculated figure over unverified data must show
-- both facts. Pre-IOE rows stay NULL ("basis not recorded") rather than being
-- back-filled with a guess.
ALTER TABLE reco.recommendation
    ADD CONSTRAINT recommendation_calculation_basis_check
    CHECK (calculation_basis IS NULL OR calculation_basis IN
        ('engine_determined','rule_formula_determined',
         'scenario_estimate','projection_estimate')) NOT VALID;
ALTER TABLE reco.recommendation VALIDATE CONSTRAINT recommendation_calculation_basis_check;

ALTER TABLE reco.recommendation
    ADD CONSTRAINT recommendation_evidence_status_check
    CHECK (evidence_status IS NULL OR evidence_status IN
        ('documented_verified','documented_unverified',
         'user_attested','incomplete')) NOT VALID;
ALTER TABLE reco.recommendation VALIDATE CONSTRAINT recommendation_evidence_status_check;

-- ---- Widen the lifecycle vocabulary (superset; no data rewrite) -------------
-- New states: new, saved, planned, dismissed, unable_to_complete, expired.
-- Legacy states (generated, viewed, accepted, rejected, completed) are RETAINED
-- so existing rows validate and no migration of historical data is needed;
-- 'generated' is read as 'new' at the application boundary.
-- Match the CHECK constraint that constrains exactly the `status` COLUMN.
-- (Matching on the constraint definition text would be ambiguous: the
-- evidence_status constraint added above also contains the substring "status".)
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
         WHERE con.conrelid = 'reco.recommendation'::regclass
           AND con.contype = 'c'
           AND con.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                    WHERE a.attrelid = con.conrelid
                                      AND a.attname = 'status')]
    LOOP
        EXECUTE format('ALTER TABLE reco.recommendation DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE reco.recommendation
    ADD CONSTRAINT recommendation_status_check
    CHECK (status IN (
        -- legacy (retained)
        'generated','viewed','accepted','rejected','completed',
        -- contract-v2 lifecycle
        'new','saved','planned','dismissed','unable_to_complete','expired'
    )) NOT VALID;
ALTER TABLE reco.recommendation VALIDATE CONSTRAINT recommendation_status_check;

-- The append-only lifecycle event log accepts the same superset.
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
         WHERE con.conrelid = 'reco.recommendation_status_event'::regclass
           AND con.contype = 'c'
           AND con.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                    WHERE a.attrelid = con.conrelid
                                      AND a.attname = 'status')]
    LOOP
        EXECUTE format('ALTER TABLE reco.recommendation_status_event DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE reco.recommendation_status_event
    ADD CONSTRAINT recommendation_status_event_status_check
    CHECK (status IN (
        'generated','viewed','accepted','rejected','completed',
        'new','saved','planned','dismissed','unable_to_complete','expired'
    )) NOT VALID;
ALTER TABLE reco.recommendation_status_event
    VALIDATE CONSTRAINT recommendation_status_event_status_check;
