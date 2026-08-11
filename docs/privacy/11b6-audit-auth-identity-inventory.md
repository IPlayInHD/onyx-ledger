# Entry 11B6 — audit & authentication identity inventory

Discovery for PD-15 (durable audit ownership) and PD-3 (authentication
identifiers). Read out of a live PostgreSQL 16 database built from
`backend/db/sql`, not from the models — the models are a second opinion, and
where the two disagree the database is what the deletion worker will meet.

## 1. Identity-bearing durable surfaces

| Surface | Identity columns | RLS | Who may write | Purpose |
|---|---|---|---|---|
| `identity.login_event` | `user_id` (FK, `ON DELETE SET NULL`), `email_tried` (**citext, plaintext**), `ip_address` (inet), `user_agent` | **none** | `onyx_app_rw` INSERT/UPDATE/DELETE | authentication evidence |
| `identity.auth_session` | `user_id`, `refresh_token_hash`, `ip_address`, `user_agent` | none | `onyx_app_rw` full CRUD | session/revocation |
| `audit.audit_log` | `actor_type`, `actor_id`, `entity_id` (text), `previous_value`/`new_value` (jsonb), `ip_address` | none | `onyx_audit_writer` **INSERT only** | append-only change history |
| `audit.security_event` | `user_id`, `detail` (jsonb), `ip_address` | none | `onyx_app_rw` INSERT only | security evidence |
| `audit.consent_log` | `user_id`, `ip_address` | none | `onyx_app_rw` INSERT only | consent record |
| `audit.data_deletion_request` | `user_id`, `reason` | none | — | deletion ledger (PD-9) |
| `audit.data_export_request` | `user_id`, `object_key` | none | — | export ledger (11B8, absent) |
| `identity.password_reset_token` | `user_id`, `token_hash` | none | — | credential lifecycle |
| `identity.email_verification_token` | `user_id`, `token_hash` | none | — | credential lifecycle |
| `identity.mfa_method` | `user_id`, `secret_kms_ref`, `label` | none | — | authentication factor |

The absence of RLS on `audit.*` and most of `identity.*` is deliberate and was
recorded under PD-1: these are not tenant-scoped tables, and 11 of the 27
user-derived tables "correctly cannot have it". It is why deletion here has to
be a governed keyhole rather than an RLS-scoped DELETE.

## 2. What PD-15 actually is, in this schema

`audit.audit_log` has **`actor_id` and no subject column**. The row records who
acted; the account the action was *about* is recoverable only from
`entity_id` — a `text` column — or from the payload. So:

- an anonymous registration or login writes `actor_id = NULL`, and the only
  trace of which person it concerned is inside the payload;
- an operator acting on a customer records the OPERATOR in `actor_id`, and a
  de-identification keyed on `actor_id` would either miss the customer or
  wrongly rewrite the operator;
- a worker-generated event has no natural actor at all.

A de-identification pass keyed on `actor_id` alone is therefore both incomplete
and unsafe. That is the defect, stated in schema terms.

## 3. Write paths that create the problem

`app/services/auth/service.py`:

- success — `LoginEvent(user_id=..., event_type="success", ip_address=ip)`;
  no email is stored, which is already right.
- failure — `_record_login_failure` writes
  `LoginEvent(user_id=..., email_tried=email, event_type="failure",
  ip_address=ip)` in **its own committed transaction**, deliberately: Entry 11A
  found the failure row was being rolled back with the rejected login (PD-6).
  That commit-before-raise shape must be preserved; only what it stores changes.

A failed login against a NON-existent account has `user_id = NULL` and a
plaintext `email_tried`. It is the clearest case of identity that no
`user_id`-keyed deletion can ever reach.

## 4. Field classification

Using the categories the entry defines:

| Field | Class |
|---|---|
| `login_event.event_type`, `created_at` | STRUCTURAL_METADATA |
| `login_event.user_id` | SUBJECT identity — erasable via the account |
| `login_event.email_tried` | PERSONAL_IDENTITY_TO_DEIDENTIFY |
| `login_event.ip_address` | SHORT_RETENTION_SECURITY_EVIDENCE |
| `login_event.user_agent` | SHORT_RETENTION_SECURITY_EVIDENCE |
| `auth_session.refresh_token_hash` | SECRET_ADJACENT — never widened, never copied |
| `audit_log.actor_id` | ACTOR identity — preserved for operator accountability |
| `audit_log.entity_id`, payload | SUBJECT identity, unstructured today |
| `security_event.detail` | free-form payload — must be field-aware guarded |

## 5. Integration point — already reserved

`identity.account_lifecycle_phase` already constrains

    phase IN ('SOURCE_DATA', 'DOCUMENTS', 'AUDIT_AUTH_DEIDENTIFICATION')

and `SourceDataPhase` already names `AUDIT_AUTH_DEIDENTIFICATION`. Entry 11B5
built the durable phase machinery — claim, lease, recovery, attempt ceiling,
append-only phase rows — and reserved this name for this work. There is no
second orchestrator to build, and building one would be the wrong answer.

## 6. Deployment status

`PRODUCTION_DATA_DEPLOYMENT_UNKNOWN`. Nothing in the repository evidences a
deployment holding real user data, and no claim is made here that historical
production rows have been remediated. Any backfill this entry provides is a
mechanism, and its having been *run* is a separate operational fact.
