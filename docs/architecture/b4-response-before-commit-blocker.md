# Entry B4 — PRODUCTION_DEFECT: a success can reach the client before its commit

Found while certifying B4. The legal-acceptance subsystem is not the cause and
is not the only victim: **every write endpoint in this application can return a
success response before the transaction behind it has committed.**

B4's own goal statement is the reason this is recorded as a blocker rather than
a curiosity:

> THE SYSTEM MUST NEVER CLAIM ACCEPTANCE THAT WAS NOT SUCCESSFULLY PERSISTED.

A 200 delivered before the commit is exactly that claim, made by the request
lifecycle rather than by any one service.

## How it surfaced

`seedPersona` in the browser suite registers, signs in, confirms the address,
then reads `GET /api/v1/legal/state`. In CI on `90f8305` that read was refused:

    POST /api/v1/auth/verification/confirm -> 200   15:24:09.434
    GET  /api/v1/legal/state              -> 403   15:24:09.439

    {"type":"https://onyx.ledger/errors/email-verification-required",
     "title":"Email Verification Required","status":403, ...}

2.6 ms after being told the address was confirmed, the account read as
`pending_verification`. It failed on the first attempt and again on the
automatic retry — two accounts, two tokens, the same answer.

It does not reproduce on an idle server. 150 attempts across three shapes
(sequential, four-way concurrent, and one keep-alive connection) produced zero
failures. It reproduces under the load of a full parallel browser suite.

## What was measured

A real uvicorn server, a route whose only dependency is a `yield` dependency
that records `COMMIT` in its teardown after a 2 ms delay — the cost of one
round trip to PostgreSQL. A client calls `/write` over a keep-alive connection
and, the moment it holds the 200, asks the server what has happened:

| Middleware | Responses delivered BEFORE the teardown ran |
|---|---:|
| `BaseHTTPMiddleware` (what this app uses) | **12 / 40** |
| pure-ASGI middleware | **24 / 40** |

The application's `CorrelationIdMiddleware` is a `BaseHTTPMiddleware`, and
`unit_of_work` commits in exactly that teardown:

```python
async def db_anon() -> AsyncIterator[AsyncSession]:
    async with unit_of_work(actor_type="system") as session:
        yield session          # commit happens when this generator closes
```

So the client can hold a 200 for a write that has not landed, and its next
request — a browser's `recheck()`, a customer's reload, the next call in a
sequence — can read the pre-commit state.

## Two hypotheses, both refuted

Recorded because each looked right and neither was, and the next person should
not spend the time again.

**1. "The request log line proves the commit finished."** It does not. The log
line is written by the middleware after `call_next` returns, which is *before*
the dependency teardown. Measured ordering, `TestClient`:

    1. handler body
    2. middleware after call_next   <-- the request log line
    3. dependency teardown          <-- the commit
    4. client has the response

Every timestamp in the API log therefore overstates how early the commit
happened. The 2.6 ms gap above is measured against the wrong event.

**2. "`BaseHTTPMiddleware` is the cause; pure ASGI fixes it."** It is not, and
it does not — pure ASGI measured *worse* (24/40). The window is in where
FastAPI closes the dependency exit stack relative to the response reaching the
socket on a real server, not in the middleware style. `TestClient` cannot
observe it at all: under `TestClient` the commit precedes the response in all
three configurations, which is why no unit test catches this.

## Why it was not fixed in B4

The repair belongs in the request lifecycle, not in the legal subsystem, and
not in a test helper. The shape that would actually close it is a unit-of-work
owner that commits **before** `http.response.start` is sent — buffering the
response until the write is durable. That touches every request in the
application, has to be right about streaming responses, error paths and
background tasks, and is a larger and riskier change than the entry that found
it should be making.

B4's DO NOT list does not forbid it; the entry boundary does. Recorded, scoped,
and handed on rather than half-done.

## What the browser suite does in the meantime

`legalStateAfterVerification` in `frontend/e2e/account.ts` reads the state
through a bounded settle, and only for the one refusal this defect produces.
Any other 403 surfaces immediately, and an account that never becomes readable
fails the test. It is a tolerance with a name and a citation, not a sleep.

## Blast radius

Every endpoint that writes. The customer-visible shapes include:

- confirming an email address, then being asked to confirm it again
- accepting the Terms, then being asked to accept them again
- saving a figure, reloading, and seeing the previous value

None of these lose data — the commit does land — but each is the product
telling a customer something that is not yet true, which for a tax product is
the failure mode that matters most.

## Status

**OPEN.** Launch-blocking in the author's judgement: it predates B4, survived
B1–B3 certification unnoticed, and affects the whole write surface.
