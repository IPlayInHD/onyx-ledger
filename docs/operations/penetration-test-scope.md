# Independent penetration test — scope package

For an external tester. **This is a scope document, not a result.** Onyx has not
had an independent penetration test; nothing in this repository should be read
as claiming one. Internal adversarial testing was performed by the team and
found and fixed what it found, which is a different and weaker thing.

**Not ready to execute yet:** the environments described below are designed and
not provisioned, so there is no host to point a scanner at. Send this when
staging exists.

---

## 1. What Onyx is

A Canadian tax intelligence product. Customers enter income, expenses and
registered-account facts; a certified Python/FastAPI backend computes a federal
and provincial position, records governed opportunities, and models what a
single decision would change. A React frontend displays those figures and
computes none of its own.

Launch coverage is Federal + Ontario.

**The property most worth attacking** is not availability. It is whether one
customer can reach another customer's tax data, and whether anything can make
the product state a figure the certified engine did not produce.

## 2. Assets in scope

| Asset | Notes |
|---|---|
| `https://<staging-frontend>` | Static SPA behind a CDN |
| `https://<staging-api>` | FastAPI, `/api/v1/*`, OpenAPI at `/openapi.json` |
| Authentication | Register, login, refresh rotation, logout, password reset |
| Admission control | Per-identity and per-source-IP rate and concurrency limits |
| Tenant isolation | PostgreSQL row level security, per-request `app.user_id` |
| Object storage | Pre-signed URL issuance and expiry |
| Webhooks | Payment provider callback endpoint |
| AI explanation | `POST /api/v1/ai/explanations` |
| Admin and publication surfaces | Separate credentials, four-eyes publication |

Hostnames are filled in when staging is provisioned.

## 3. Explicitly out of scope

- **Production.** Staging only. Production holds real customer tax records.
- The archived prototype under `legacy/`. It is not deployed and is not part of
  the product; findings against it are not findings.
- Third-party providers themselves — payment, email, AI, cloud. Their
  *integration* is in scope; their infrastructure is theirs.
- Destructive testing: no data deletion beyond your own test accounts, no
  sustained volumetric denial of service, no social engineering of staff, no
  physical testing.

Rate-limit testing **is** wanted, bounded: prove the control exists and where it
sits, do not try to exhaust it.

## 4. Test accounts

Four supplied on request, all with synthetic data:

| Account | Purpose |
|---|---|
| `pentest-a@…` | Ordinary customer with a full tax position |
| `pentest-b@…` | Second customer — the cross-tenant target |
| `pentest-suspended@…` | Suspended, for status enforcement |
| `pentest-admin@…` | Administrative surface, least privilege |

Two ordinary accounts are supplied on purpose: cross-tenant access is the
finding we most want you to look for, and it needs a real second tenant rather
than a fabricated identifier.

## 5. What we believe is true

Stated so you can try to disprove it. Each was tested internally; internal
testing is exactly what an external test exists to check.

1. One customer cannot read or write another's data through any endpoint. RLS
   is `ENABLE` + `FORCE`, the runtime role does not hold `BYPASSRLS`, and
   `app.user_id` is set per transaction from the access token.
2. `404` is non-enumerating: "not yours" and "not there" are the same answer.
3. A suspended, closed or deleted account cannot authenticate, refresh, or use
   an access token still inside its lifetime.
4. Refresh tokens are single-use; reuse revokes the whole session family.
5. No ambient authority: cookies are never sent, authorisation is an explicit
   header, so CSRF has nothing to borrow.
6. No inline scripts or styles; CSP is `script-src 'self'; style-src 'self'`.
7. The AI layer cannot become an authority. The model receives a validated
   input contract only — no database, no retrieval, no web, no admin context —
   and a failed validation falls back to a deterministic renderer. It decides no
   amount.
8. Error bodies are RFC-9457, correlated, and carry no stack trace, SQL
   fragment or internal identifier. `/readyz` returns a verdict and no reason.
9. The tax engine is the only authority for a customer-facing figure.

## 6. Attack classes we most want covered

Cross-tenant access and IDOR; RLS bypass; authentication and session handling
including refresh replay and fixation; privilege escalation toward the
publication and admin roles; injection; mass assignment; rate-limit bypass;
webhook forgery and replay; SSRF from any server-side fetch; pre-signed URL
scope and expiry; prompt injection aimed at making the explanation layer state a
figure the engine did not produce; and secret exposure in the bundle or in
responses.

Infrastructure: publicly reachable database or Redis, public object storage,
cloud IAM over-permission, instance metadata access, backup access, and exposed
admin interfaces.

## 7. Reporting

Findings with a concrete reproduction path, please. **Severity should reflect a
demonstrated path, not a theoretical one** — that is the standard the internal
audit held itself to, and mixed-confidence findings are hard to triage.

Anything you judge critical: contact immediately rather than waiting for the
report. Contact and escalation details are supplied with the engagement rather
than committed here.

## 8. What we already know is missing

Told up front rather than discovered, because your time is better spent
elsewhere:

- Document upload is scaffolded and returns 501.
- Email verification is not implemented; accounts are created active.
- Payments and entitlement are not implemented.
- Multi-factor authentication is not implemented for customers or admins.
- Legal-acceptance persistence is not implemented.

These are known gaps, not findings. If one of them is reachable in a way we did
not expect, that **is** a finding.
