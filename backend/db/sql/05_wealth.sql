-- =============================================================================
-- Onyx Ledger — 05 · Assets & Liabilities  (schema: wealth)
-- Alembic revision: 0006_wealth
-- Balance-sheet items with time-series history. current_* columns cache the
-- latest child row for cheap reads; the child tables are authoritative.
-- =============================================================================

-- ---- Assets -----------------------------------------------------------------
CREATE TABLE wealth.asset (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    asset_category_id   uuid NOT NULL REFERENCES ref.asset_category(id),
    label               text NOT NULL,
    acquisition_date    date,
    acquisition_cost    ref.money_amt,               -- adjusted cost base seed
    current_value       ref.money_amt,               -- cached latest valuation
    current_value_as_of date,
    currency_code       text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);
CREATE INDEX ix_asset_user ON wealth.asset (user_id) WHERE deleted_at IS NULL;
CREATE INDEX ix_asset_category ON wealth.asset (asset_category_id);

CREATE TABLE wealth.asset_valuation (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    asset_id    uuid NOT NULL REFERENCES wealth.asset(id) ON DELETE CASCADE,
    as_of_date  date NOT NULL,
    value       ref.money_amt NOT NULL,
    source      text,                                -- 'user','statement','market'
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_asset_valuation ON wealth.asset_valuation (asset_id, as_of_date);
CREATE INDEX ix_asset_valuation_asset ON wealth.asset_valuation (asset_id, as_of_date DESC);

-- Subtype (1:1) for registered accounts: TFSA/RRSP/FHSA/RESP contribution room.
CREATE TABLE wealth.registered_account_detail (
    asset_id            uuid PRIMARY KEY REFERENCES wealth.asset(id) ON DELETE CASCADE,
    registered_type     text NOT NULL REFERENCES ref.account_registered_type(code),
    tax_year            ref.tax_year_num NOT NULL,
    contribution_room   ref.money_amt,
    contributions_ytd   ref.money_amt NOT NULL DEFAULT 0,
    withdrawals_ytd     ref.money_amt NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---- Liabilities ------------------------------------------------------------
CREATE TABLE wealth.liability (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    liability_category_id uuid NOT NULL REFERENCES ref.liability_category(id),
    provider            text,
    principal_amount    ref.money_amt,
    current_balance     ref.money_amt,               -- cached latest balance
    current_balance_as_of date,
    interest_rate       ref.rate,
    status              text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','paid_off','in_default','closed')),
    opened_on           date,
    currency_code       text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);
CREATE INDEX ix_liability_user ON wealth.liability (user_id) WHERE deleted_at IS NULL;

CREATE TABLE wealth.liability_balance (
    id           uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    liability_id uuid NOT NULL REFERENCES wealth.liability(id) ON DELETE CASCADE,
    as_of_date   date NOT NULL,
    balance      ref.money_amt NOT NULL,
    interest_rate ref.rate,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_liability_balance ON wealth.liability_balance (liability_id, as_of_date);
CREATE INDEX ix_liability_balance_liab ON wealth.liability_balance (liability_id, as_of_date DESC);
