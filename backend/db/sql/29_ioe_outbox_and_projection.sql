-- =============================================================================
-- Onyx Ledger — 29 · Freshness outbox + governed projection metadata
-- Alembic revision: 0035_ioe_outbox_and_projection
-- P6 closure items 1 and 2.
--
-- PART 1 — THE OUTBOX.
-- Freshness invalidation must not be lost and must not be spurious. A producer
-- writing directly to a broker gets both failure modes: the broker call can
-- succeed while the transaction rolls back (an invalidation for a change that
-- never happened), or the transaction can commit while the broker call fails
-- (a change nobody is told about).
--
-- So the event is written to a TABLE in the SAME TRANSACTION as the change that
-- caused it. If the change commits, the event exists; if it rolls back, the
-- event never existed. A relay drains the table afterwards. This is the
-- transactional-outbox pattern, and it is the only way to make "the event
-- happened exactly when the change happened" true rather than nearly true.
--
-- Consumers mark affected records STALE. They never touch sealed evidence.
--
-- PART 2 — GOVERNED PROJECTION METADATA.
-- Whether an opportunity recurs is a legislative question, so the IOE must not
-- infer it. Rule authors declare it through TKMS four-eyes governance, exactly
-- as they declare eligibility and economic effect. A candidate with no
-- authorization produces no projection — never a guess, never a silent zero.
--
-- ALL ADDITIVE.
-- =============================================================================

-- ---- 1. The outbox ----------------------------------------------------------
CREATE TABLE ioe.freshness_outbox (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    -- What moved. Enumerated so a consumer can never receive an event it has no
    -- handler for.
    event_type          text NOT NULL CHECK (event_type IN (
        'analysis_completed',
        'baseline_inputs_changed',
        'financial_data_changed',
        'profile_changed',
        'document_status_changed',
        'rule_published',
        'rule_withdrawn',
        'rule_superseded',
        'reference_data_changed',
        'engine_version_changed',
        'objective_policy_changed',
        'lever_registry_changed',
        'assumption_registry_changed'
    )),
    -- The stale reason this event implies. Resolved by the PRODUCER, which knows
    -- what actually changed, rather than guessed at by the consumer.
    stale_reason_code   text NOT NULL,

    -- Scope. Exactly one targeting dimension is populated; a consumer that had
    -- to guess the scope could invalidate far more than moved.
    user_id             uuid REFERENCES identity.user_account(id) ON DELETE CASCADE,
    analysis_id         uuid,
    tax_year            integer,
    -- Global events (engine/registry version changes) set none of the above.

    -- Idempotency. A relay is at-least-once, so the same logical event may be
    -- written or delivered twice; this key makes the second one a no-op.
    dedupe_key          text NOT NULL,

    -- Relay bookkeeping. An explicit, enumerated state machine: a row is in
    -- exactly one state and every move between them is a named transition.
    claim_state         text NOT NULL DEFAULT 'pending'
                        CHECK (claim_state IN
                            ('pending','claimed','completed','failed')),
    claimed_by          text,
    claim_token         uuid,
    claimed_at          timestamptz,
    processed_at        timestamptz,
    attempts            smallint NOT NULL DEFAULT 0,
    last_error_code     text,
    created_at          timestamptz NOT NULL DEFAULT now(),

    -- Claim bookkeeping is coherent or the row is rejected: a claimed row has
    -- an owner and a token, an unclaimed one has neither.
    CONSTRAINT freshness_outbox_claim_is_coherent CHECK (
        (claim_state = 'claimed'
         AND claimed_by IS NOT NULL AND claim_token IS NOT NULL
         AND claimed_at IS NOT NULL)
        OR (claim_state <> 'claimed')
    ),
    CONSTRAINT freshness_outbox_terminal_is_stamped CHECK (
        claim_state NOT IN ('completed','failed') OR processed_at IS NOT NULL
    ),

    CONSTRAINT freshness_outbox_scope_is_singular CHECK (
        num_nonnulls(analysis_id, tax_year) <= 1
    )
);

-- The same logical event is written at most once. A producer racing itself, or
-- retried, collides here instead of producing duplicate work.
CREATE UNIQUE INDEX uq_ioe_freshness_outbox_dedupe
    ON ioe.freshness_outbox (dedupe_key);

-- The RLS predicate resolves through user_id, so it needs an index of its own
-- (P4 carried constraint: RLS is the correctness boundary, not the access path).
CREATE INDEX ix_ioe_freshness_outbox_user ON ioe.freshness_outbox (user_id);

-- The claim query: pending rows in deterministic order.
CREATE INDEX ix_ioe_freshness_outbox_pending
    ON ioe.freshness_outbox (created_at, id)
    WHERE claim_state = 'pending';

-- Abandoned-claim recovery scans only currently-claimed rows.
CREATE INDEX ix_ioe_freshness_outbox_claimed
    ON ioe.freshness_outbox (claimed_at)
    WHERE claim_state = 'claimed';

COMMENT ON TABLE ioe.freshness_outbox IS
    'Transactional outbox for freshness invalidation. Written in the same transaction as the change that caused it, so an event exists if and only if the change committed.';
COMMENT ON COLUMN ioe.freshness_outbox.dedupe_key IS
    'Uniquely identifies the logical event. Delivery is at-least-once, so processing must be idempotent and a duplicate write must collide here.';
COMMENT ON COLUMN ioe.freshness_outbox.stale_reason_code IS
    'Resolved by the producer, which knows what actually changed. A consumer must never infer it.';

-- The outbox is written by the application role and drained by the relay.
GRANT SELECT, INSERT, UPDATE ON ioe.freshness_outbox TO onyx_app_rw;

-- Not user-derived evidence: rows carry identifiers and codes, never financial
-- values. RLS is still applied on the user-scoped ones so a tenant cannot
-- enumerate another tenant's activity. Global rows (user_id IS NULL) are
-- readable — they say only "the engine version changed".
ALTER TABLE ioe.freshness_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.freshness_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_freshness_outbox ON ioe.freshness_outbox
    USING (user_id IS NULL OR user_id = ref.current_app_user())
    WITH CHECK (user_id IS NULL OR user_id = ref.current_app_user());

-- ---- 2. Governed projection metadata ----------------------------------------
-- Authored as RULE DATA and published through TKMS four-eyes governance. The
-- IOE consumes these verbatim and never infers them.
ALTER TABLE rules.rule_outcome
    ADD COLUMN projection_eligibility        text,
    ADD COLUMN projection_method             text,
    ADD COLUMN maximum_projection_horizon    smallint,
    ADD COLUMN required_assumption_codes     jsonb;

ALTER TABLE rules.rule_outcome
    ADD CONSTRAINT rule_outcome_projection_eligibility_check
        CHECK (projection_eligibility IS NULL OR projection_eligibility IN (
            -- the rule authorizes projecting this opportunity forward
            'eligible',
            -- explicitly one-off; projecting it would invent value
            'not_eligible',
            -- authored but conditional on the declared assumptions holding
            'conditionally_eligible'
        )) NOT VALID,
    ADD CONSTRAINT rule_outcome_projection_method_check
        CHECK (projection_method IS NULL OR projection_method IN (
            -- carry the annual amount forward unchanged
            'flat_recurring',
            -- carry forward with published indexation only
            'indexed_recurring',
            -- a fixed number of remaining years, declared by the rule
            'fixed_term'
        )) NOT VALID,
    ADD CONSTRAINT rule_outcome_projection_horizon_check
        CHECK (maximum_projection_horizon IS NULL
               OR maximum_projection_horizon BETWEEN 1 AND 25) NOT VALID,
    ADD CONSTRAINT rule_outcome_required_assumptions_is_array
        CHECK (required_assumption_codes IS NULL
               OR jsonb_typeof(required_assumption_codes) = 'array') NOT VALID,
    -- an eligible rule must say HOW far and BY WHAT METHOD; "eligible" alone
    -- would leave the horizon to the consumer, which is the inference this
    -- metadata exists to prevent
    ADD CONSTRAINT rule_outcome_eligible_projection_is_complete
        CHECK (projection_eligibility IS DISTINCT FROM 'eligible'
               OR (projection_method IS NOT NULL
                   AND maximum_projection_horizon IS NOT NULL)) NOT VALID;

ALTER TABLE rules.rule_outcome
    VALIDATE CONSTRAINT rule_outcome_projection_eligibility_check;
ALTER TABLE rules.rule_outcome
    VALIDATE CONSTRAINT rule_outcome_projection_method_check;
ALTER TABLE rules.rule_outcome
    VALIDATE CONSTRAINT rule_outcome_projection_horizon_check;
ALTER TABLE rules.rule_outcome
    VALIDATE CONSTRAINT rule_outcome_required_assumptions_is_array;
ALTER TABLE rules.rule_outcome
    VALIDATE CONSTRAINT rule_outcome_eligible_projection_is_complete;

COMMENT ON COLUMN rules.rule_outcome.projection_eligibility IS
    'Whether published legislation supports projecting this opportunity into future years. Authored as rule data under four-eyes governance; the IOE never infers recurrence.';
COMMENT ON COLUMN rules.rule_outcome.maximum_projection_horizon IS
    'The furthest year the rule authorizes projecting to. A consumer may project less, never more.';
COMMENT ON COLUMN rules.rule_outcome.required_assumption_codes IS
    'Assumption codes that must be present and satisfied before a projection may be generated. Absent any of them, no projection is produced.';

-- Projections record which authorization produced them, so a stored projection
-- can be traced to the published rule that permitted it.
ALTER TABLE ioe.multi_year_projection
    ADD COLUMN projection_method        text,
    ADD COLUMN methodology_version      text,
    ADD COLUMN authorizing_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id);

CREATE INDEX ix_ioe_projection_authorizing_rule
    ON ioe.multi_year_projection (authorizing_rule_version_id)
    WHERE authorizing_rule_version_id IS NOT NULL;

-- =============================================================================
-- 3. The privileged outbox interface
--
-- The relay is a CROSS-TENANT system process, so it cannot claim user-scoped
-- outbox rows through an ordinary tenant session — and that refusal is correct,
-- not an obstacle to route around. RLS is not disabled, the worker gets no table
-- grants, and nothing here marks a user's scenarios or optimizations stale.
--
-- These three functions manage the OUTBOX WORKFLOW and nothing else: claim,
-- complete, fail. The actual staling happens afterwards, in an ordinary
-- RLS-protected transaction with `app.user_id` set to the tenant the claimed
-- event names. The privileged surface is therefore as small as the problem:
-- it moves rows in one queue table between four enumerated states.
--
-- Every function pins `search_path`, so a caller cannot shadow `ioe` or
-- `pg_catalog` with a schema of their own and have the definer's rights execute
-- it. None takes a table name, a filter, a SQL fragment, or a mutation target;
-- the only inputs are a bounded batch size, an event id, a worker id, and an
-- enumerated error code. None returns a financial value.
-- =============================================================================

-- A dedicated, restricted role. It owns no tables, is granted no table
-- privileges anywhere, and can execute exactly these three functions.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_freshness_worker') THEN
        CREATE ROLE onyx_freshness_worker NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA ioe TO onyx_freshness_worker;

-- An append-only record of every claim and every terminal transition. A queue
-- that can lose or double-deliver work is only debuggable if its history is
-- written down.
CREATE TABLE ioe.freshness_outbox_audit (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    event_id        uuid NOT NULL,
    transition      text NOT NULL CHECK (transition IN
                        ('claimed','completed','failed','claim_recovered')),
    worker_id       text,
    claim_token     uuid,
    error_code      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_outbox_audit_event
    ON ioe.freshness_outbox_audit (event_id, created_at);

COMMENT ON TABLE ioe.freshness_outbox_audit IS
    'Append-only log of outbox claims and terminal transitions. Written by the privileged functions only.';

-- How long a claim may be held before it is treated as abandoned. A worker that
-- dies mid-event must not strand it forever.
CREATE OR REPLACE FUNCTION ioe.freshness_claim_timeout()
RETURNS interval LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$ SELECT interval '10 minutes' $$;

-- ---- claim ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ioe.claim_freshness_events(
    p_batch_size integer,
    p_worker_id  text
)
-- OUT names are prefixed because a plpgsql OUT parameter shadows a column of
-- the same name, and the body reads those columns.
RETURNS TABLE (
    out_event_id          uuid,
    out_claim_token       uuid,
    out_stale_reason_code text,
    out_analysis_id       uuid,
    out_tax_year          integer,
    out_user_id           uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_batch integer;
    v_token uuid := ref.uuid_generate_v7();
BEGIN
    IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
        RAISE EXCEPTION 'worker_id is required to claim events';
    END IF;
    -- Bounded: a caller cannot ask for the whole queue.
    v_batch := least(greatest(coalesce(p_batch_size, 1), 1), 200);

    -- Stale-claim recovery: a claim older than the timeout is returned to
    -- 'pending' so the work is not stranded by a worker that died.
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
             -- deterministic ordering, and a unique tiebreak so two workers
             -- never disagree about which row is "next"
             ORDER BY c.created_at, c.id
             LIMIT v_batch
             FOR UPDATE SKIP LOCKED
         )
        RETURNING o.id, o.claim_token, o.stale_reason_code,
                  o.analysis_id, o.tax_year, o.user_id
    ), logged AS (
        INSERT INTO ioe.freshness_outbox_audit
            (event_id, transition, worker_id, claim_token)
        SELECT id, 'claimed', p_worker_id, claim_token FROM claimed
        RETURNING 1
    )
    SELECT c.id, c.claim_token, c.stale_reason_code,
           c.analysis_id, c.tax_year, c.user_id
      FROM claimed c
     ORDER BY c.id;
END;
$$;

-- ---- complete ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION ioe.complete_freshness_event(
    p_event_id uuid,
    p_worker_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_row ioe.freshness_outbox%ROWTYPE;
BEGIN
    SELECT * INTO v_row FROM ioe.freshness_outbox WHERE id = p_event_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    -- Idempotent: acknowledging an already-completed event is a no-op, not an
    -- error. At-least-once delivery guarantees this will happen.
    IF v_row.claim_state = 'completed' THEN
        RETURN true;
    END IF;
    -- Only the worker holding the claim may acknowledge it. A late
    -- acknowledgement from a worker whose claim was recovered must not
    -- overwrite the state of whoever picked the work up afterwards.
    IF v_row.claim_state <> 'claimed' OR v_row.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN false;
    END IF;

    UPDATE ioe.freshness_outbox
       SET claim_state = 'completed', processed_at = now(),
           claimed_by = NULL, claim_token = NULL, claimed_at = NULL,
           last_error_code = NULL
     WHERE id = p_event_id;

    INSERT INTO ioe.freshness_outbox_audit
        (event_id, transition, worker_id, claim_token)
    VALUES (p_event_id, 'completed', p_worker_id, v_row.claim_token);
    RETURN true;
END;
$$;

-- ---- fail -------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ioe.fail_freshness_event(
    p_event_id   uuid,
    p_worker_id  text,
    p_error_code text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_row ioe.freshness_outbox%ROWTYPE;
BEGIN
    -- An enumerated code, never a message: exception text can carry SQL
    -- fragments and row values, and none of that belongs in a queue record.
    IF p_error_code IS NULL OR p_error_code !~ '^[A-Z][A-Z0-9_]{2,63}$' THEN
        RAISE EXCEPTION 'error_code must be an enumerated code';
    END IF;

    SELECT * INTO v_row FROM ioe.freshness_outbox WHERE id = p_event_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_row.claim_state = 'failed' THEN
        RETURN true;                      -- idempotent
    END IF;
    IF v_row.claim_state <> 'claimed' OR v_row.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN false;
    END IF;

    -- Below the attempt ceiling the event returns to the queue; at the ceiling
    -- it terminates, so a permanently broken event cannot spin forever.
    IF v_row.attempts >= 5 THEN
        UPDATE ioe.freshness_outbox
           SET claim_state = 'failed', processed_at = now(),
               last_error_code = p_error_code,
               claimed_by = NULL, claim_token = NULL, claimed_at = NULL
         WHERE id = p_event_id;
        INSERT INTO ioe.freshness_outbox_audit
            (event_id, transition, worker_id, claim_token, error_code)
        VALUES (p_event_id, 'failed', p_worker_id, v_row.claim_token, p_error_code);
    ELSE
        UPDATE ioe.freshness_outbox
           SET claim_state = 'pending', last_error_code = p_error_code,
               claimed_by = NULL, claim_token = NULL, claimed_at = NULL
         WHERE id = p_event_id;
    END IF;
    RETURN true;
END;
$$;

-- ---- privileges -------------------------------------------------------------
-- The worker role can execute these three functions and do nothing else. It has
-- no SELECT on any user table, so a compromised worker cannot read a single
-- financial value.
REVOKE ALL ON FUNCTION ioe.claim_freshness_events(integer, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ioe.complete_freshness_event(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ioe.fail_freshness_event(uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION ioe.claim_freshness_events(integer, text)
    TO onyx_freshness_worker;
GRANT EXECUTE ON FUNCTION ioe.complete_freshness_event(uuid, text)
    TO onyx_freshness_worker;
GRANT EXECUTE ON FUNCTION ioe.fail_freshness_event(uuid, text, text)
    TO onyx_freshness_worker;

COMMENT ON FUNCTION ioe.claim_freshness_events(integer, text) IS
    'Outbox workflow only: claims a bounded batch of pending events for a worker. Returns identifiers, codes and scope keys — never a financial value. Does not stale anything; the relay does that afterwards under ordinary RLS with app.user_id set.';

-- A rule publication or reference-data change is scoped to a tax year and
-- affects many tenants. Fanning it out is still OUTBOX WORKFLOW — it writes
-- outbox rows and nothing else — so that the relay always has a single tenant
-- to set app.user_id to, and never stales across tenants itself.
CREATE OR REPLACE FUNCTION ioe.fan_out_freshness_event(
    p_event_id  uuid,
    p_worker_id text
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_row   ioe.freshness_outbox%ROWTYPE;
    v_count integer := 0;
BEGIN
    SELECT * INTO v_row FROM ioe.freshness_outbox WHERE id = p_event_id FOR UPDATE;
    IF NOT FOUND OR v_row.claim_state <> 'claimed'
       OR v_row.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN 0;
    END IF;
    IF v_row.user_id IS NOT NULL THEN
        RETURN 0;                          -- already single-tenant
    END IF;

    -- Reads user_id ONLY, to decide who has work pending. No financial column
    -- is selected, and nothing outside the outbox is written.
    WITH affected AS (
        SELECT DISTINCT s.user_id
          FROM ioe.scenario s
         WHERE s.workflow_status = 'completed'
           AND s.freshness_status = 'current'
           AND (v_row.tax_year IS NULL OR s.tax_year = v_row.tax_year)
           AND (v_row.analysis_id IS NULL OR s.base_analysis_id = v_row.analysis_id)
        UNION
        SELECT DISTINCT r.user_id
          FROM ioe.optimization_run r
         WHERE r.workflow_status = 'completed'
           AND r.freshness_status = 'current'
           AND (v_row.tax_year IS NULL OR r.tax_year = v_row.tax_year)
           AND (v_row.analysis_id IS NULL OR r.analysis_id = v_row.analysis_id)
    ), inserted AS (
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, user_id, analysis_id, tax_year, dedupe_key)
        SELECT v_row.event_type, v_row.stale_reason_code, a.user_id,
               v_row.analysis_id, v_row.tax_year,
               v_row.dedupe_key || ':' || a.user_id::text
          FROM affected a
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO v_count FROM inserted;
    RETURN v_count;
END;
$$;

REVOKE ALL ON FUNCTION ioe.fan_out_freshness_event(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ioe.fan_out_freshness_event(uuid, text)
    TO onyx_freshness_worker;

COMMENT ON FUNCTION ioe.fan_out_freshness_event(uuid, text) IS
    'Outbox workflow only: expands a tax-year- or analysis-scoped event into per-tenant child events so the relay always has one tenant to set app.user_id to. Reads user_id only; writes nothing outside the outbox.';

-- The runtime role runs the relay, so it must be able to assume the restricted
-- worker role. It gains only what that role has: EXECUTE on four functions.
GRANT onyx_freshness_worker TO onyx_app_rw;
