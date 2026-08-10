# Entry 11B5I — external and durable surface closure matrix (§21–§32)

Established from current code, configuration and the dependency lock at
`129a9d6`. Where a claim rests on something absent, the absence was checked
rather than assumed.

## Matrix

| Surface | Holds 11B5 live source/profile data? | Durable? | Deletion mechanism | Owning entry | 11B5 disposition |
|---|---|---|---|---|---|
| **PostgreSQL — live source** (`finance.income_source`, `finance.expense_record`, `profile.tax_profile`, `profile.user_profile`, `profile.spouse_profile`, `profile.dependent`, `wealth.asset`, `wealth.liability`) | **Yes** | Yes | `identity.purge_source_data`, gated by `count_remaining_source_data` | **11B5** | **COVERED** |
| PostgreSQL — sealed history (`analysis_input_snapshot`, `optimization_run`, `scenario`, `strategy_portfolio`) | Frozen copies, deliberately retained | Yes | Retained by policy; proven unchanged by deletion in 11B5H3 | 11B5H3 | **RETAINED BY DESIGN** — not a forgotten copy |
| PostgreSQL — audit metadata (`audit.audit_log`) | No values; minimized payload | Yes | 11B0 payload minimization; de-identification is a later phase | 11B0 / later phase | OUT OF SOURCE_DATA SCOPE |
| PostgreSQL — freshness outbox (`ioe.freshness_outbox`) | Identifiers and codes only | Yes | Bounded retry then terminal; proven value-free in H2D | 11B4/H2D | NO VALUES |
| PostgreSQL — durable lifecycle ledger (`identity.account_lifecycle`, `_phase`, `_event`) | No | Yes — outlives the account (PD-9) | Retained deliberately | 11B3 | RETAINED BY DESIGN |
| **Object storage** | **No** — document binaries only | Yes | Document lifecycle (PD-2/PD-8) | **11B4** | NOT A SOURCE-DATA SURFACE |
| **Redis / Celery** | **No** | Results off | `task_ignore_result=True`, `task_store_errors_even_if_ignored=False`, `result_expires=24h`; privacy task takes no subject argument | 11A | **NO DURABLE COPY** |
| **Logs** | No values | Depends on shipper | Closed codes + identifiers; raw-exception logging tracked separately | 11A / separate PD | NO VALUES IN LIFECYCLE PATHS |
| **Node `server/`** | Its own separate copies | Yes (`server/data/db.json` or Netlify Blobs) | **None yet — PD-14 open, assigned to 11B9** | **PD-14 / 11B9** | **SEPARATE_APPLICATION** — see below |
| **AI / model provider** | **No** | n/a | No provider client exists | — | **NO EXTERNAL AI COPY** |
| Temporary / export files | No user data | n/a | `scripts/export_privacy_inventory.py` emits registry *metadata* (table names, classes), never tenant rows | — | NOT_PRESENT |

## Node `server/` — classification: `SEPARATE_APPLICATION`

Evidence, from the repository as it stands:

* **Its own runtime.** `server/package.json` is an Express 4 app
  (`onyx-ledger-engine`), deployed by `netlify.toml` as a serverless function
  with `base = "server"`. The Python backend is a separate deployment.
* **No shared database.** A grep for `postgres`, `pg.`, `Pool(` across
  `server/*.js`, `server/engine/`, `server/netlify/` returns **nothing**.
  Persistence is `server/store.js` → a JSON file store (`server/data/db.json`)
  or Netlify Blobs under Lambda.
* **No shared identity or authentication.** It issues its own JWTs
  (`jsonwebtoken`) against its own bcrypt hashes (`bcryptjs`) over its own
  `db.users` map. A `user_id` there is unrelated to a PostgreSQL `user_account.id`.
* **No shared object storage.** Netlify Blobs, not the backend's bucket.
* **Deleting a PostgreSQL account therefore implies nothing there**, because
  there is no identifier by which the two could be joined.

So Entry 11B5's SOURCE_DATA lifecycle does not — and structurally cannot —
reach it, and it is not an Entry 11B5 blocker.

**It is still a real privacy gap, and it stays open.** `PD-14` already records
it ("`server/` is a second application storing emails, bcrypt hashes, profiles
and documents, and was absent from the first inventory pass",
`IMPLEMENTATION_GAP`, MEDIUM) and the specification assigns reconciliation to
**11B9**. Nothing in Entry 11B5I resolves PD-14, and this entry does not claim
to. The distinction to hold onto:

```
Entry 11B5 lifecycle coverage   = the Python/PostgreSQL backend's source data
PD-14 / 11B9                    = the Node application's own store
```

## Redis / Celery (§28)

* `celery_app.conf.task_ignore_result = True` — nothing is written to the
  result backend on success.
* `task_store_errors_even_if_ignored = False` — the setting that would put
  exception payloads back into Redis is explicitly off. Entry 11A established
  why: a SQLAlchemy `DBAPIError.__str__` renders the failing statement *and its
  parameters*, so a database error during a financial write would otherwise put
  the amount into Redis.
* `result_expires = 24h`, explicit rather than inherited.
* `run_account_deletion_phases(worker_id="privacy-worker")` takes **no subject
  argument** — it claims its own work from the database, so no user id or
  financial value is ever a task argument in the broker payload.
* Route: `workers.tasks.privacy.run_account_deletion_phases → queue "privacy"`.
  Lifecycle work indirectly depends on `ioe_freshness` (relay) and
  `ioe_integrity` (verification), both explicitly routed.

## AI / model provider (§30)

**NO EXTERNAL AI COPY**, verified three ways rather than from the old inventory:

1. `app/integrations/llm.py` ships `TemplateLlmClient` — deterministic,
   in-process, and by construction never emits a dollar figure. The Anthropic
   adapter in that file is **commented-out example code**.
2. `app/integrations/embeddings.py` ships `LocalHashEmbedder` — feature hashing,
   no network.
3. **No provider SDK is installed at all**: `anthropic` and `openai` appear zero
   times in `requirements.lock.txt`, `requirements-dev.lock.txt` and
   `pyproject.toml`.

`settings.llm_provider` / `llm_model` are configuration placeholders for a
future adapter; no client is constructed from them today.

## Worker runtime provisioning (§21)

| Setting | Principal | Used by |
|---|---|---|
| `ONYX_DATABASE_URL` | `onyx_app_rw` | HTTP request path |
| `ONYX_PRIVACY_DATABASE_URL` | `onyx_privacy_worker` | account deletion phases |
| `ONYX_FRESHNESS_DATABASE_URL` | `onyx_freshness_worker` | freshness relay **and the integrity scheduler** (since H2D) |

Both privileged settings have no default and no fallback to
`ONYX_DATABASE_URL`; a missing one raises `WorkerRuntimeUnavailable` carrying
the runtime name and never the DSN. A fallback would reintroduce PD-16 by
configuration rather than by grant.

## Forgotten live copies within Entry 11B5 scope

**NO.** Every durable representation of live source/profile/financial data
inside the Python/PostgreSQL backend is either purged by
`identity.purge_source_data` or is a deliberately retained sealed artifact whose
immutability across deletion was proven in 11B5H3. The one customer-data store
outside that boundary is the Node application, which is `PD-14`/`11B9` and is
recorded as still open.
