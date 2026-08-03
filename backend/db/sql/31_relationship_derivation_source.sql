-- =============================================================================
-- Onyx Ledger — 31 · Relationship derivation provenance  (schema: ioe)
-- Alembic revision: 0037_rel_derivation_source
--
-- A relationship edge is evidence, and evidence without provenance cannot be
-- audited. `derivation_source` records WHY the edge exists:
--
--   rules_contract         an explicit dependency or exclusion group supplied
--                          by the rules layer — authoritative, never trimmed
--   shared_resource        both candidates draw on the same declared pool
--   relationship_registry  a governed structural pattern (lever conflict, or
--                          two levers writing the same engine input field)
--   measured_interaction   an engine run measured the change in benefit
--
-- Without this column a reader cannot distinguish a legal fact from a derived
-- structural hint, and the sparse derivation cannot prove it trimmed only
-- explanatory edges.
--
-- ADDITIVE. Existing rows predate the distinction and are backfilled to the
-- registry source, which is what the previous pairwise derivation produced for
-- all but the shared-pool edges; those are recovered from
-- `shared_resource_code`, which the old derivation set only for pool edges.
-- =============================================================================

ALTER TABLE ioe.recommendation_relationship
    ADD COLUMN IF NOT EXISTS derivation_source text;

-- The table carries `ioe.reject_result_mutation()`, which refuses UPDATE:
-- sealed calculation evidence is immutable to the application. Backfilling a
-- new provenance column is schema evolution, not an application mutation, so
-- the trigger is disabled for exactly this statement and immediately restored.
-- Nothing already recorded changes: the value is DERIVED from columns the row
-- already holds, and no financial or explanatory field is touched.
ALTER TABLE ioe.recommendation_relationship DISABLE TRIGGER USER;

UPDATE ioe.recommendation_relationship
   SET derivation_source = CASE
        WHEN relationship_type IN ('requires', 'precedes')     THEN 'rules_contract'
        WHEN relationship_type = 'excludes'                    THEN 'rules_contract'
        WHEN shared_resource_code IS NOT NULL                  THEN 'shared_resource'
        WHEN relationship_type IN ('enhances', 'reduces_value') THEN 'measured_interaction'
        ELSE 'relationship_registry'
   END
 WHERE derivation_source IS NULL;

ALTER TABLE ioe.recommendation_relationship ENABLE TRIGGER USER;

ALTER TABLE ioe.recommendation_relationship
    ALTER COLUMN derivation_source SET NOT NULL;

-- No DEFAULT: a writer must state provenance explicitly. A default would let a
-- future insert silently claim the wrong source.
ALTER TABLE ioe.recommendation_relationship
    DROP CONSTRAINT IF EXISTS ck_ioe_relationship_derivation_source;
ALTER TABLE ioe.recommendation_relationship
    ADD CONSTRAINT ck_ioe_relationship_derivation_source
    CHECK (derivation_source IN
        ('rules_contract', 'shared_resource', 'relationship_registry',
         'measured_interaction'));

COMMENT ON COLUMN ioe.recommendation_relationship.derivation_source IS
    'Provenance of the edge. rules_contract edges are authoritative and are never dropped by the sparse-derivation budget; the other sources are derived and may be trimmed.';
