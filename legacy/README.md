# Legacy prototypes — DECOMMISSIONED. Do not deploy.

Everything under this directory is a **retired prototype**. It is kept because
it is part of the project's history and because the privacy specification has
to be able to name what it once stored. It is not part of the product, it is
not maintained, and it must never serve a customer again.

## What is here

| Path | What it was |
|---|---|
| `server/` | A Node/Express application with its own tax engine, its own accounts, and its own storage (Netlify Blobs) |
| `static-site/` | A zero-backend build of the same engine, running entirely in the browser |

## Why it was decommissioned

**Onyx must have exactly one customer tax authority.** That authority is the
certified Python/FastAPI backend in `backend/`, whose figures are governed,
versioned, replayable and gated. `server/engine/` is a *second, independent*
implementation of Canadian tax logic in JavaScript. Two engines cannot both be
right, and nothing reconciles them — a customer served by this one would be
shown numbers no certified run ever produced.

It also held a second copy of customer identity. The privacy specification
records this as **PD-14**: `server/store.js` stored email addresses, bcrypt
password hashes, profiles and documents outside the governed data lifecycle, so
an account deletion performed by the real product would not have reached it.

Until this commit, the repository's root `netlify.toml` deployed **this** tree
and routed every `/api/*` request to it. The certified backend was not deployed
at all. Connecting the repository to Netlify — which the old `NETLIFY.md`
explicitly instructed — would have put the legacy engine in front of real
people.

## The rules now

1. **No deployment configuration may reference this directory.** A test
   enforces it: `backend/tests/security/test_legacy_engine_not_deployable.py`.
2. **No build script may install or bundle it.**
3. If you need to read it for history, read it. If you find yourself wanting to
   run it, the answer is `backend/` plus `frontend/`.

Deleting it outright was considered and rejected: the privacy specification
still refers to these files when describing what data the retired application
held, and that record is worth more than the few kilobytes.
