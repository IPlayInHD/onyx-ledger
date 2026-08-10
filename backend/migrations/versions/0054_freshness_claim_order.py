"""0054_freshness_claim_order — applies backend/db/sql/48_freshness_claim_order.sql

Returns a claimed batch in the order it was selected (created_at, id) instead
of by id alone. Ids come from `ref.uuid_generate_v7`, which is millisecond
precision plus randomness, so same-millisecond events were handed to the relay
in random order — 48 of 200 trials came back reversed, against 0 after — and
the first-cause-wins freshness reason a user is shown was decided by that coin
flip.

DOWNGRADE restores the id-only ordering, which reintroduces the scrambling.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0054_freshness_claim_order"
down_revision = "0053_pd16_freshness_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("48_freshness_claim_order.sql")


def downgrade() -> None:
    # Reinstates the scrambled batch order. See the module docstring.
    #
    # The OUT row must match what 36_freshness_scope_and_reasons.sql installed,
    # `out_jurisdiction` included. An earlier draft of this downgrade copied the
    # SUPERSEDED shape from 29_ioe_outbox_and_projection.sql; PostgreSQL
    # rejected it outright ("cannot change return type of existing function"),
    # so the rollback would have failed at the worst possible moment rather
    # than quietly dropping the jurisdiction qualifier.
    op.execute("""
        CREATE OR REPLACE FUNCTION ioe.claim_freshness_events(
            p_batch_size integer,
            p_worker_id  text
        )
        RETURNS TABLE (
            out_event_id          uuid,
            out_claim_token       uuid,
            out_stale_reason_code text,
            out_analysis_id       uuid,
            out_tax_year          integer,
            out_jurisdiction      text,
            out_user_id           uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ioe, pg_catalog
        AS $fn$
        DECLARE
            v_batch integer;
            v_token uuid := ref.uuid_generate_v7();
        BEGIN
            IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
                RAISE EXCEPTION 'worker_id is required to claim events';
            END IF;
            v_batch := least(greatest(coalesce(p_batch_size, 1), 1), 200);

            WITH recovered AS (
                UPDATE ioe.freshness_outbox
                   SET claim_state = 'pending',
                       claimed_by = NULL, claim_token = NULL, claimed_at = NULL
                 WHERE claim_state = 'claimed'
                   AND claimed_at < now() - ioe.freshness_claim_timeout()
                RETURNING id, claimed_by, claim_token
            )
            INSERT INTO ioe.freshness_outbox_audit
                (event_id, transition, worker_id, claim_token)
            SELECT id, 'claim_recovered', claimed_by, claim_token FROM recovered;

            RETURN QUERY
            WITH claimed AS (
                UPDATE ioe.freshness_outbox o
                   SET claim_state = 'claimed',
                       claimed_by = p_worker_id,
                       claim_token = v_token,
                       claimed_at = now(),
                       attempts = o.attempts + 1
                 WHERE o.id IN (
                    SELECT c.id FROM ioe.freshness_outbox c
                     WHERE c.claim_state = 'pending'
                       AND c.attempts < 5
                     ORDER BY c.created_at, c.id
                     LIMIT v_batch
                     FOR UPDATE SKIP LOCKED
                 )
                RETURNING o.id, o.claim_token, o.stale_reason_code,
                          o.analysis_id, o.tax_year, o.jurisdiction, o.user_id
            ), logged AS (
                INSERT INTO ioe.freshness_outbox_audit
                    (event_id, transition, worker_id, claim_token)
                SELECT id, 'claimed', p_worker_id, claim_token FROM claimed
                RETURNING 1
            )
            SELECT c.id, c.claim_token, c.stale_reason_code,
                   c.analysis_id, c.tax_year, c.jurisdiction, c.user_id
              FROM claimed c
             ORDER BY c.id;
        END;
        $fn$;
    """)
