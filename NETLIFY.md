# Deploying Onyx Ledger to Netlify

This repo is already configured for Netlify. The root **`netlify.toml`**:

- builds from `server/` (`npm install`),
- publishes the app frontend from `server/public/`,
- runs the Express tax-audit API as a serverless function (`server/netlify/functions/api.js`),
- routes `/api/*` to that function,
- and persists accounts/audits in **Netlify Blobs** (auto-provisioned — no database to set up).

You just need to connect the repo to your Netlify account. Two ways:

---

## Option A — Connect the Git repo (recommended, continuous deploy)

1. Go to **app.netlify.com → Add new site → Import an existing project**.
2. Choose **GitHub** and pick **`IPlayInHD/onyx-ledger`** (authorize Netlify if prompted).
3. Netlify reads `netlify.toml` automatically — leave the build settings as detected:
   - Base directory: `server`
   - Build command: `npm install`
   - Publish directory: `server/public` (shown as `public` relative to base)
   - Functions directory: `server/netlify/functions`
4. Pick the branch to deploy. This work is on **`claude/zen-hypatia-8cxiza`** — either
   set that as the production branch, or merge it into `main` first and deploy `main`.
5. Click **Deploy**. When it finishes you'll get a `*.netlify.app` URL. Open it, create an
   account, add the sample slips, and run an audit.

After the first deploy, every push to that branch redeploys automatically.

## Option B — Netlify CLI

```bash
npm install -g netlify-cli
netlify login                 # opens your browser to authorize
cd /path/to/onyx-ledger
netlify init                  # link this repo to a new or existing site
netlify deploy --build --prod # build + deploy to production
```

---

## Recommended: set a stable JWT secret

Sign-in tokens are signed with a secret. If you don't provide one, the app generates a
random secret and stores it in Netlify Blobs (fine, but it can rotate on a fresh Blobs
store). For stable sessions, set your own:

- **Site settings → Environment variables → Add** `JWT_SECRET` = *(a long random string)*
- or `netlify env:set JWT_SECRET "$(openssl rand -hex 32)"`

## Netlify Blobs

No setup required — Blobs is automatically available to your functions. The store
(`server/store.js`) detects the serverless runtime and uses Blobs there; locally
(`npm start`) it uses a JSON file. Accounts, documents, and audits are namespaced under
the `onyx-ledger` blob store.

## Good to know

- **Local dev** is unchanged: `cd server && npm start` → http://localhost:4000.
- **One complete site.** The publish directory `server/public` now contains everything:
  the marketing landing (`/`), the interactive demos (`/app.html`, `/copilot.html`,
  `/tax-health-score.html`), and the real product (`/signup.html`, `/login.html`,
  `/dashboard.html`). The homepage's "Start free" / plan CTAs funnel into the real
  signup → dashboard → audit flow.
- **Scanned-image OCR** still needs an OCR provider wired into the `ocrProvider` interface
  in `server/engine/extract.js`; today it scans structured entries and text/PDF-text slips.
- **This does not file taxes** and is not affiliated with the CRA — it's educational.
