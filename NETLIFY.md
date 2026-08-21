# Deploying the Onyx frontend to Netlify

Netlify hosts **only the static frontend**. It is a CDN for `frontend/dist` and
nothing else. The tax engine, the accounts and the data live in the certified
FastAPI backend, which is deployed separately and is the single authority for
every figure a customer sees.

> **This file used to say something different.** It described deploying
> `server/` — a second, independent JavaScript tax engine with its own accounts
> and its own storage — and routing every `/api/*` request to it. That tree is
> archived under `legacy/` and can no longer be built or served. See
> `legacy/README.md`.

## What the build does

`netlify.toml` builds from `frontend/`:

```
npm ci && npm run build && node scripts/netlify-redirects.mjs
```

- `npm ci` installs from the lockfile, so a deploy resolves the same dependency
  tree the quality gate tested.
- `npm run build` typechecks and emits `dist/`.
- `scripts/netlify-redirects.mjs` writes `_redirects` and `_headers` for this
  particular deploy, because both depend on which backend it talks to.

## Required configuration

| Variable | Example | Why |
|---|---|---|
| `ONYX_API_ORIGIN` | `https://api.onyxledger.ca` | The certified backend this deploy proxies `/api/*` to, and the only origin its CSP allows it to contact |

**The build fails if `ONYX_API_ORIGIN` is unset, or if it is not `https://`.**
That is deliberate. A frontend published without an API still renders, still
shows a sign-in form, and cannot authenticate anyone — it looks deployed and is
not. A plain-text origin would put bearer tokens and tax figures on the wire in
the clear.

Set it per context (production, deploy previews, branch deploys) so a preview
build cannot point at the production backend.

## What ships with it

Set in `netlify.toml`: `X-Content-Type-Options`, `Referrer-Policy`,
`X-Frame-Options`, `Permissions-Policy`, the cross-origin isolation headers, and
HSTS (two years, subdomains; **preload is deliberately not enabled** until the
domain is settled, because preload is very hard to undo).

Generated per-deploy: the `Content-Security-Policy`, whose `connect-src` names
the backend origin above. It is strict on both halves —

```
script-src 'self'; style-src 'self'
```

— with no `'unsafe-inline'` anywhere. The frontend carries no inline styles and
no inline scripts, and a unit test keeps it that way.

## What is NOT decided here

Netlify is the frontend host. It is not the backend host, not the database, not
the queue, and not the secret manager. Those belong to the production
architecture, and until that is provisioned this deploy has nothing to talk to.
See `docs/operations/production-architecture.md`.
