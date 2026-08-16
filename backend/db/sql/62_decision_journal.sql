-- =============================================================================
-- Entry: Tax Decision Journal (migration 0068)
-- =============================================================================
--
-- WHAT THIS IS. The durable record of what a user DECIDED about their tax
-- position: considered, proceed, defer, decline — and what they REPORTED doing
-- about it. Two tables:
--
--   ioe.decision_journal        one row per decision thread; immutable identity
--                               plus the pinned artifact references that say
--                               what the user was looking at when they decided
--   ioe.decision_journal_event  append-only history; a change of mind is a new
--                               event, never an UPDATE of an old one
--
-- WHO IS THE AUTHORITY. The USER, for intent and self-report. Nothing in these
-- tables is computed: no tax figure, no eligibility verdict, no readiness. The
-- thread PINS identities (scenario id, sealed result hash, comparison hash,
-- schema versions) so the sealed artifacts stay authoritative for what was on
-- the screen; it copies none of their content.
--
-- APPEND-ONLY BY GRANTS, NOT BY TRIGGER — a deliberate departure from
-- `trg_immutable`, with a reason: these rows die with the account through the
-- `identity.user_account` cascade fired by `identity.terminal_remove_account`,
-- and that function does not set `app.allow_evidence_purge` — it cascades into
-- tables the privacy phases already emptied. A GUC-gated DELETE trigger here
-- would abort terminal removal. Revoking UPDATE/DELETE from `onyx_app_rw`
-- closes the same door for the application (RLS already scopes what it can
-- see), while referential actions — which run with row security bypassed as
-- the table owner — still purge the rows the moment the account row goes.
--
-- WHY THE EVENT CARRIES user_id. RLS on the event table joins to the thread
-- for ownership (the scenario_event convention). The thread's own policy uses
-- its user_id directly. No denormalized user_id on events: one owner column,
-- one place to be wrong.

CREATE TABLE ioe.decision_journal (
    id                              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                         uuid NOT NULL
                                    REFERENCES identity.user_account(id) ON DELETE CASCADE,
    scenario_id                     uuid NOT NULL
                                    REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    -- The optional decision subject: a governed opportunity code, verbatim.
    subject_opportunity_code        text,
    -- Pinned artifact identities. What the user considered is the sealed
    -- artifact; these say WHICH sealed artifact, so later versions of anything
    -- cannot quietly rewrite what informed the decision.
    scenario_result_hash            text,
    scenario_result_schema_version  text,
    comparison_hash                 text,
    comparison_schema_version       text,
    -- Client-supplied creation identity: the idempotency key. A retried POST
    -- lands on this constraint and returns the thread it already created.
    request_id                      uuid NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, request_id)
);

CREATE INDEX ix_ioe_decision_journal_user
    ON ioe.decision_journal (user_id, created_at DESC, id);
CREATE INDEX ix_ioe_decision_journal_scenario
    ON ioe.decision_journal (scenario_id);

COMMENT ON TABLE ioe.decision_journal IS
    'One user decision thread about one sealed scenario. Immutable after '
    'insert; everything that happens next is an event. Dies with the account '
    'via the user_account cascade (LIVE_USER_DATA_DELETE).';

CREATE TABLE ioe.decision_journal_event (
    id                          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    journal_id                  uuid NOT NULL
                                REFERENCES ioe.decision_journal(id) ON DELETE CASCADE,
    -- Dense per-thread ordering, assigned under the thread's row lock. THE
    -- ordering authority: two concurrent appends serialize on the lock, and
    -- the unique constraint makes a lost race an error rather than a tie.
    sequence                    integer NOT NULL CHECK (sequence >= 1),
    event_type                  text NOT NULL CHECK (event_type IN
                                    ('CREATED', 'DECISION_RECORDED', 'ACTION_REPORTED')),
    -- The user's declared intent. Present exactly when the event declares one:
    -- CREATED opens at CONSIDERING (explicitly, not by implication), and
    -- DECISION_RECORDED carries the new declaration.
    decision                    text CHECK (decision IN
                                    ('CONSIDERING', 'PROCEED', 'DEFER', 'DECLINE')),
    -- The date the user SAYS the action happened, when they reported one.
    -- Their claim, validated for sanity at the API — never a rewrite of
    -- recorded_at, which stays the server''s own clock.
    user_reported_action_date   date,
    event_schema_version        text NOT NULL DEFAULT '1.0.0',
    request_id                  uuid NOT NULL,
    recorded_at                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (journal_id, sequence),
    UNIQUE (journal_id, request_id),
    -- Shape: a decision travels only on decision-bearing events; a reported
    -- action date only on an action report. Named to match the ORM metadata,
    -- so schema drift compares them as the same constraint.
    CONSTRAINT ck_decision_on_decision_events CHECK
        ((decision IS NOT NULL) = (event_type IN ('CREATED', 'DECISION_RECORDED'))),
    CONSTRAINT ck_action_date_on_action_reports CHECK
        (user_reported_action_date IS NULL OR event_type = 'ACTION_REPORTED')
);

CREATE INDEX ix_ioe_decision_journal_event_thread
    ON ioe.decision_journal_event (journal_id, sequence);

COMMENT ON TABLE ioe.decision_journal_event IS
    'Append-only decision history. A change of mind appends; nothing here is '
    'ever updated, and the application role cannot delete. PROCEED means the '
    'user declared an intent; ACTION_REPORTED means the user said they acted. '
    'Neither is system verification, and nothing in this table claims it is.';

-- ---------------------------------------------------------------------------
-- Row-level security: the thread by its own user_id, the events through the
-- thread — the same two shapes 26_ioe_result_rls uses for scenario children.
-- ---------------------------------------------------------------------------
ALTER TABLE ioe.decision_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.decision_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_decision_journal ON ioe.decision_journal
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

ALTER TABLE ioe.decision_journal_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.decision_journal_event FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_decision_journal_event ON ioe.decision_journal_event
    USING (EXISTS (SELECT 1 FROM ioe.decision_journal j
                    WHERE j.id = journal_id
                      AND j.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (SELECT 1 FROM ioe.decision_journal j
                         WHERE j.id = journal_id
                           AND j.user_id = ref.current_app_user()));

-- ---------------------------------------------------------------------------
-- Privileges. 21_ioe's default privileges granted the application role full
-- DML on new ioe tables; append-only means taking UPDATE and DELETE back.
-- INSERT + SELECT is the entire application surface.
-- ---------------------------------------------------------------------------
REVOKE UPDATE, DELETE ON ioe.decision_journal FROM onyx_app_rw;
REVOKE UPDATE, DELETE ON ioe.decision_journal_event FROM onyx_app_rw;
