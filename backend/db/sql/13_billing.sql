-- =============================================================================
-- Onyx Ledger — 13 · Subscriptions & Billing  (schema: billing)
-- Alembic revision: 0014_billing
-- Plans/subscriptions/invoices. Payment instruments are provider TOKENS only —
-- never PANs. Billing's DB role has no access to finance/profile PII.
-- =============================================================================

CREATE TABLE billing.plan (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,                -- 'free','pro','advisor'
    name        text NOT NULL,
    price_amount ref.money_amt NOT NULL DEFAULT 0,
    currency_code text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    billing_interval text NOT NULL DEFAULT 'month'
                    CHECK (billing_interval IN ('month','year','once')),
    features    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- evolving feature flags
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE billing.subscription (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                 uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    plan_id                 uuid NOT NULL REFERENCES billing.plan(id),
    status                  text NOT NULL DEFAULT 'trialing'
                            CHECK (status IN ('trialing','active','past_due','canceled','expired')),
    provider                text NOT NULL DEFAULT 'stripe',
    provider_subscription_id text,
    current_period_start    timestamptz,
    current_period_end      timestamptz,
    cancel_at               timestamptz,
    canceled_at             timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_subscription_user ON billing.subscription (user_id);
CREATE UNIQUE INDEX uq_subscription_active ON billing.subscription (user_id)
    WHERE status IN ('trialing','active','past_due');

CREATE TABLE billing.invoice (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    subscription_id     uuid NOT NULL REFERENCES billing.subscription(id) ON DELETE CASCADE,
    amount              ref.money_amt NOT NULL,
    currency_code       text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    status              text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','paid','void','uncollectible')),
    provider_invoice_id text,
    issued_at           timestamptz,
    paid_at             timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_invoice_subscription ON billing.invoice (subscription_id);

-- Tokenized payment method reference (NO card numbers).
CREATE TABLE billing.payment_method_ref (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    provider            text NOT NULL DEFAULT 'stripe',
    provider_customer_id text,
    provider_method_id  text,                        -- pm_xxx token
    brand               text,                        -- 'visa'
    last4               char(4),
    exp_month           smallint,
    exp_year            smallint,
    is_default          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_payment_method_user ON billing.payment_method_ref (user_id);

-- Derived feature entitlements (evolving flags → JSONB).
CREATE TABLE billing.entitlement (
    user_id     uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    features    jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_subscription_id uuid REFERENCES billing.subscription(id) ON DELETE SET NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
