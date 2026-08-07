# Production Engineering Quality Gate (Entry 7)

Onyx Ledger calculates numbers people act on. The calculation correctness work
(Items 3A/3B, Entries 8/9) established that a sealed result can be reproduced
from the inputs it names. This entry is about the layer underneath: whether the
code that does the calculating, the dependencies it runs on, and the schema it
reads can be *relied upon to be what they claim to be* — automatically, on every
change, without anyone remembering to check.

Four things were missing, and each one is a way for a correct system to become
an incorrect one without anyone noticing:

| Gap | How it goes wrong |
|---|---|
| Type checking was advisory and partial | A wrong row type, a `None` where a `Decimal` was assumed, an unchecked function body — none of it stops anything |
| Dependencies were declared as ranges, never locked | Development, CI, and production each resolve a different SQLAlchemy, and a determinism guarantee measured on one is not a guarantee on another |
| No CI existed at all | Every gate was "a developer runs it locally, probably" |
| `alembic autogenerate` reported ~300 differences | A comparison full of known noise cannot report an unknown change |

---

## 1. Engineering inventory (before this entry)

| Area | State |
|---|---|
| Language / runtime | Python, `requires-python = ">=3.11"` — a range, not a pin |
| Package manager | pip + `pyproject.toml`. No `requirements.txt`, no lock artifact, no hashes |
| Lint | Ruff, configured in `pyproject.toml`, clean |
| Types | mypy, configured with `ignore_missing_imports = true` and **no strictness flags at all** |
| Tests | pytest, 724 passing against a freshly provisioned PostgreSQL |
| Schema | `backend/db/sql/*.sql` authoritative; Alembic chain mirrors it 1:1 |
| CI | **None.** No `.github/` directory existed |
| Container image | None |
| Release process | Undefined |

## 2. Pre-entry baseline (measured)

```
fresh-database tests      724 passed
Ruff (app workers)        clean
global mypy (app workers) 58 errors in 20 files
protected-scope mypy      53 errors            (app/services/ioe app/services/tax_engine app/database workers)
migration head            0042_freshness_scope_and_reasons
alembic autogenerate      298 remove_constraint
                           12 modify_comment
                            4 remove_table_comment
                            2 modify_nullable
structural drift          zero (no table/column/index diffs under the migration lens)
```

---

## 3. The protected typing scope

### 3.1 What is protected

**Everything.** The protected scope is the package roots `app` and `workers` —
the entire shipped application, 188 source files.

This is deliberate. A scope expressed as a subset creates a boundary, and a
boundary is somewhere a defect can be moved to escape the gate. There is no
"unprotected" directory to relocate a stubborn module into.

The scope is declared once, in `pyproject.toml`:

```toml
[tool.onyx.quality_gate]
protected_scope = ["app", "workers"]
```

`scripts/check_types.sh`, the CI workflow, and
`tests/unit/test_quality_gate_policy.py` all read it from there, so they cannot
disagree about what is protected.

### 3.2 Why package roots, not a file list

A file list has to be maintained, and the moment someone forgets, a new module
is outside the gate while looking like it is inside. Package roots include new
modules the moment they exist.

`tests/unit/test_quality_gate_policy.py` enforces this structurally:

- every importable top-level package that is not test/migration/tooling code
  must be under a protected root — adding `app2/` or a new top-level service
  package fails the test;
- the calculation-critical packages are additionally named one by one, so a
  future scope change cannot drop them silently;
- `ignore_errors` is banned outright, in the global config and in every
  override;
- the strictness flags the zero-error claim depends on are asserted present.

### 3.3 Coverage of the required responsibilities

| Responsibility | Package | Protected |
|---|---|---|
| Tax calculations | `app/services/tax_engine` | ✓ |
| Rules / eligibility evaluation | `app/services/tax_engine/rules_service.py`, `app/services/ioe/portfolio/eligibility.py` | ✓ |
| Optimization orchestration | `app/services/ioe/orchestrator.py` | ✓ |
| Portfolio evaluation | `app/services/ioe/portfolio`, `app/services/ioe/domain/portfolio.py` | ✓ |
| Scenario execution | `app/services/ioe/scenario` | ✓ |
| Frozen snapshot reconstruction | `app/services/ioe/frozen` | ✓ |
| Replay verification | `app/services/ioe/replay` | ✓ |
| Integrity scheduling | `app/services/ioe/replay/scheduler.py` | ✓ |
| Freshness producers / relay / fan-out | `app/services/ioe/freshness_events.py`, `freshness_relay.py`, `scenario/freshness_service.py` | ✓ |
| Canonicalization / hashing | `app/services/ioe/domain/canonical.py` | ✓ |
| Database session / RLS context | `app/database` | ✓ |
| IOE workers | `workers` | ✓ |
| API DTOs / domain contracts | `app/schemas`, `app/services/tax_engine/contracts.py` | ✓ |
| TKMS ingestion / governance | `app/services/tkms` | ✓ |
| AI explanation (never calculates) | `app/services/ai` | ✓ |

### 3.4 The strictness that makes zero mean something

Reaching zero errors under a permissive configuration is easy and worthless.
The configuration is in `[tool.mypy]`:

| Flag | Why it is on |
|---|---|
| `check_untyped_defs` | **The important one.** By default mypy does not check the *bodies* of unannotated functions at all. Without this, "no issues found" can coexist with an entirely unchecked module |
| `disallow_untyped_defs`, `disallow_incomplete_defs` | Every function declares its types; a half-annotated signature is not accepted |
| `disallow_untyped_calls` | Calling into an unchecked function from a checked one is an error, so untyped code cannot spread by being called |
| `warn_return_any` | A value that decayed to `Any` cannot be laundered through a declared return type — this is exactly how a wrong ORM row type reaches a caller |
| `warn_unused_ignores` | A suppression that stopped being necessary is removed rather than left as cover |
| `warn_redundant_casts`, `warn_unreachable`, `strict_equality`, `extra_checks`, `disallow_subclassing_any` | Free at this point; all pass |
| `ignore_missing_imports = false` | **Not** set globally. A blanket `true` silently turns every future untyped dependency into `Any` across the whole codebase |

Two overrides exist, both listed in `permitted_mypy_overrides` and both enforced
by the policy test:

- `celery`, `celery.*` — Celery ships no `py.typed` and has no stub package. The
  effect is confined to the `bind=True` task `self` and `.delay`/`.retry`; the
  task bodies themselves are fully annotated and checked.
- `workers.tasks.*` — `disallow_untyped_decorators` only. `@celery_app.task` is
  an untyped decorator, so mypy would report every task it wraps as untyped
  regardless of its own annotations.

Neither override sets `ignore_errors`. Neither excludes a module from checking.

### 3.5 Not adopted, and why

| Flag | Errors it would raise | Decision |
|---|---|---|
| `disallow_any_generics` | 145 in the calculation core, 200 repo-wide | Deferred. Almost all are bare `dict`/`list` in signatures. Real value, but a large mechanical diff across hash-relevant code with no behaviour change; tracked in the debt register as the next tier |
| `no_implicit_reexport` | 234 | Not adopted. These are `__init__.py` re-export conventions, a style question rather than a correctness one |
| `disallow_untyped_decorators` (globally) | 16, all Celery/FastAPI | Confined to `workers.tasks.*` as above |

---

## 4. What the type work actually found

53 protected errors became 0, and the repo-wide 58 became 0. The categories:

| Category | Before | After | Representative fix |
|---|---|---|---|
| Optional narrowing | 14 | 0 | `portfolio.py` deferred-retest branch reached `accept()` without restating the `not None` guarantee `try_admit` provides |
| Untyped dict / mapping shapes | 11 | 0 | `eligibility.py` condition-tree nodes collapsed four differently-typed keys into one union, making every `.append` and index unchecked |
| ORM result typing | 9 | 0 | `AsyncSession.scalar` is typed `-> Any`; returning it directly erased the declared row type at eight call sites |
| Context-manager typing | 1 | 0 | `unit_of_work` had no return annotation, so every `async with … as session` bound an `Any` |
| Circular annotation imports | 3 | 0 | `presentation.py` ↔ `comparison_service.py`, `replay/services.py` ↔ `domain/scenario.py`, resolved with `TYPE_CHECKING` |
| Return-type mismatches | 8 | 0 | `readyz()` declared `dict` while returning a 503 `JSONResponse` on the failure path |
| Missing annotations (untyped defs/calls) | 76 | 0 | Worker task signatures, assembler helpers, snapshot artifact builders |
| **Real runtime defects** | **3** | **0** | below |
| Other | 5 | 0 | `Literal` narrowing, `int ** int` → `Any`, open-ended band typed as closed |

### 4.1 Real runtime defects surfaced by typing

**1. TKMS extraction silently succeeded on a parse result with no stored text.**

`ParseResult.text_object_key` is nullable. `ImportService.extract()` passed it
straight into `LocalObjectStorage.get(bucket, key)`, which is typed `key: str`.

That is not a cosmetic mismatch. The object store answers a missing key with
**empty bytes**, so the parser received `""`, found no rules, and the job was
recorded as a *successful* extraction of zero rules. A governed legislation
import would have reported success while importing nothing — and because
`extract()` returns early when rows already exist, a retry would not have
recovered.

Fixed as an explicit refusal before anything is staged:

```python
if pr.text_object_key is None:
    raise ValidationError("Parse result has no stored text object to extract from")
```

Regression test:
`tests/integration/test_tkms_pipeline.py::test_extraction_refuses_a_parse_result_with_no_stored_text`.
It drives a real import to a succeeded parse, clears `text_object_key` (the
state a failed object-store write leaves behind), asserts `ValidationError`, and
asserts nothing was staged. Against the pre-fix code the call returns normally
and the assertion on `pytest.raises` fails.

**2. `ScenariosNotComparable` was raised with a `str` where it takes `list[str]`.**

`app/services/ioe/domain/freshness.py` raised
`ScenariosNotComparable("incomparable: …")`. The constructor joins its argument
with `"; "`, so a string would have been iterated *character by character* and
produced `i; n; c; o; m; p; …` as the user-visible reason for a refused scenario
comparison. On the failure path only, which is why no test had reached it.

**3. `total_future_option_value` was declared optional and is `NOT NULL`.**

The model said `Mapped[Decimal | None]`; the applied schema says `NOT NULL`. The
column is part of the portfolio canonical form and therefore of
`portfolio_result_hash`, so the model was inviting a reader to believe a
portfolio could seal without a component of its own identity. Found by the
schema-drift work, converged in the model.

### 4.2 One regression I introduced and the suite caught

Annotating `readyz() -> dict | JSONResponse` is correct for the type checker and
broke FastAPI, which tries to build a Pydantic response model from the return
annotation and refuses `JSONResponse` as a field type — **every** route test
errored at app construction. Fixed with `response_model=None` on the decorator,
keeping the truthful annotation. Reported here rather than quietly repaired
because it is the clearest evidence that the test gate does its job.

---

## 5. Suppression audit

Whole repository, after the work:

| | Count | Detail |
|---|---|---|
| New `# type: ignore` | **2** | Both `[misc]`, both in `eligibility.py`, both explained in a comment: `node.pop("_parent")` / `node.pop("_version")` on a `TypedDict`, popping the two scaffolding keys before the node is hashed. `warn_unused_ignores = true` means an unnecessary one fails the build |
| New `cast()` | **6** | 4× `cast("CursorResult[Any]", result).rowcount` (SQLAlchemy's `Result` has no `rowcount`; the DML path always returns a `CursorResult`); 1× `cast("Table", FreshnessOutbox.__table__)` (declared `FromClause` on the base, always a `Table`); 1× `cast(FilteringBoundLogger, structlog.get_logger(...))` (`get_logger` returns `Any` by design; `configure_logging` pins `wrapper_class`). **Zero `cast(Any, …)`** |
| New `Any` in signatures | **9** | `dict[str, Any]` on canonicalized payload/manifest parameters, where the payload genuinely is arbitrary JSON, and `_describe_value(value: Any) -> Any` which walks arbitrary dataclasses. None on a monetary value, a hash, an identifier, or an enumerated code |
| New `TYPE_CHECKING` imports | **3** | `presentation.py` → `LoadedComparison`; `replay/services.py` → `ScenarioSpec`; `domain/models.py` → `SavingsBreakdown` |
| New `# noqa` | **0** | |

**`TYPE_CHECKING` verification.** Each of the three is annotation-only. Every
module involved has `from __future__ import annotations`, so annotations are
strings at runtime and are never evaluated. No `isinstance`, no
`get_type_hints`, no Pydantic model, and no serialization path resolves any of
these names at runtime — `replay/services.py` additionally keeps its *runtime*
`ScenarioSpec` import local to the function that constructs one, exactly as it
did before.

---

## 6. Global mypy

```
command:  mypy app workers
config:   backend/pyproject.toml  [tool.mypy]
python:   3.11.15
```

| | Errors | Files |
|---|---|---|
| Before Entry 7 | 58 | 20 |
| After | **0** | 0 (188 source files checked) |

The command and configuration are the same in both directions, except that the
"after" configuration is materially *stricter* — the "before" 58 was measured
with no strictness flags at all. Under the current configuration the pre-entry
tree would have reported substantially more.

Because the protected scope is the whole application, "protected mypy" and
"global mypy" are the same command with the same result.

---

## 7. Dependency locking

| | |
|---|---|
| Package manager | **pip** (unchanged). `pip-compile` from pip-tools is a dev-time lock compiler, not a new installer — the lock files are plain pip requirement files |
| Python runtime | **3.11.15**, pinned |
| Lock artifacts | `backend/requirements.lock.txt` (56 packages, production), `backend/requirements-dev.lock.txt` (68 packages, production + dev) |
| Hashes | Yes — `--generate-hashes`, verified at install with `--require-hashes` |
| Regenerate | `./scripts/lock_dependencies.sh` |
| Install (local) | `pip install --require-hashes -r requirements-dev.lock.txt && pip install --no-deps -e .` |
| Install (CI/production) | identical; `--no-deps -e .` prevents the package itself pulling an unlocked resolution in behind the lock |
| Consistency gate | `./scripts/check_lock.sh` — recompiles into a temp directory and diffs |

The consistency gate is what makes this hold. Recompiling with `--no-header`
into a temporary directory and diffing means a dependency changed in
`pyproject.toml` without a relock fails, rather than depending on anyone
remembering.

### Python runtime pin

`requires-python = "==3.11.*"`, `.python-version` = `3.11.15`,
`[tool.onyx.quality_gate].python_version` = `3.11`, CI `PYTHON_VERSION` =
`3.11.15`. `scripts/check_python.sh` reads the declaration from `pyproject.toml`
and **exits non-zero with an explanation** on any other minor version; it runs
first in the release gate and before locking. There is no container image in the
repository yet, so there is no Dockerfile to pin — noted as a remaining risk.

### Clean-environment proof

`./scripts/prove_clean_install.sh` builds an *empty* virtualenv, installs from
the lock with `--require-hashes` and nothing else, imports the application,
prints the resolved versions, and runs a bounded database-free smoke test
(builds the FastAPI app, runs the pure engine, loads the Celery app). Measured:

```
python 3.11.15   sqlalchemy 2.0.51   alembic 1.19.0   fastapi 0.141.1
pydantic 2.13.4  pydantic-settings 2.14.2  asyncpg 0.31.0  psycopg2-binary 2.9.12
celery 5.6.3     redis 8.1.0        pgvector 0.5.0   structlog 26.1.0
mypy 2.3.0       ruff 0.16.1        pytest 9.1.1     pytest-asyncio 1.4.0

engine smoke: ON/2025 on 80000 employment income -> 15663.45
```

No credentials are read or printed.

---

## 8. CI

`.github/workflows/backend-quality-gate.yml`. **Every job is blocking.** There
is no `continue-on-error`, no `|| true`, and no numeric baseline anywhere in the
file.

| Job | Contains | Blocking |
|---|---|---|
| `static` | `check_python.sh`, install from lock, `check_lock.sh`, `ruff check app workers scripts tests`, `check_types.sh` (zero errors), quality-gate policy test | ✓ |
| `migrations` | fresh PostgreSQL, `check_migrations.sh` (upgrade head → downgrade base), revision hygiene, `check_schema_drift_on_fresh_db.sh`, `prove_schema_drift_gate.sh` | ✓ |
| `security` | fresh PostgreSQL, `tests/security` as `onyx_test` (member of `onyx_app_rw`) | ✓ |
| `determinism` | fresh PostgreSQL, `tests/unit/ioe`, integrity/determinism API tests, golden replay | ✓ |
| `tests` | fresh PostgreSQL, resolved-version report, full suite | ✓ |

**Permissions**: `contents: read` at workflow level. No job requests more.
**Secrets**: none. The only secret-shaped value is `ONYX_JWT_SECRET`, a
CI-only literal that exists to satisfy the settings validator and is never used
outside the workflow. No database credential, API credential, CRA credential, or
signing key appears anywhere.
**Caching**: `actions/setup-python` pip cache keyed on the interpreter version,
the runner OS, and the lock file hashes. A stale cache cannot supply a different
resolution than the lock names, and `check_lock.sh` runs regardless of cache
hits.
**Provenance**: the `tests` job prints the commit SHA, the Python version, the
resolved versions of every critical dependency, and the migration head. No
`.git` directory is required in a runtime image, and no user data is logged.
This does not duplicate the sealed IOE version manifest — that remains the
authority for what a *result* was computed under.

### Fresh database provisioning

`scripts/ci_provision_postgres.sh` installs PostgreSQL 16 + pgvector, starts it,
and creates `onyx_migrator`. Every database job provisions its own; nothing is
shared and nothing survives the runner.

The identity model is the part that matters:

| Role | Purpose | Login |
|---|---|---|
| `onyx_migrator` | Owns DDL; applies the schema. Superuser **only** because provisioning must `CREATE EXTENSION`/`CREATE ROLE` | yes (CI only) |
| `onyx_app_rw` | API runtime | NOLOGIN |
| `onyx_app_ro` | read replicas / reporting | NOLOGIN |
| `onyx_kb_admin` | tax knowledge base authoring | NOLOGIN |
| `onyx_audit_writer` | INSERT-only into `audit.*` | NOLOGIN |
| `onyx_freshness_worker` | outbox keyhole | NOLOGIN |
| `onyx_test` | **the identity the suite connects as** — a login member of `onyx_app_rw` | yes (test only) |

The suite does **not** run as a superuser. A superuser bypasses row-level
security entirely, so RLS assertions run as one prove nothing — and reading a
privilege result under the wrong identity is precisely how an earlier
investigation reached a false conclusion about the integrity keyhole functions.
Provisioning as `onyx_migrator` and testing as `onyx_app_rw` is what keeps that
from recurring.

### Security stage

`tests/security` runs as its own named, blocking stage covering `ENABLE`/`FORCE`
row level security, `USING` and `WITH CHECK` clauses, partition and
future-partition security, cross-tenant denial, deny-by-default when the tenant
GUC is unset, `SECURITY DEFINER` ownership and fixed `search_path`, `PUBLIC`
`EXECUTE` revocation, and `NOLOGIN` workers with minimal grants. The existing
authoritative tests are reused unchanged — no security logic was rewritten to
create a separate job.

---

## 9. Alembic schema-drift governance

### 9.1 The problem, stated honestly

`alembic autogenerate` will never be empty for this repository, and it should
not be. `backend/db/sql` owns indexes, foreign keys, unique and CHECK
constraints, partition children, RLS, triggers, and domains. The ORM models
declare none of them, and several cannot be modelled at all. A raw comparison
reports hundreds of differences that are correct by design.

Tolerating them by **count** is the failure mode: "298 constraint differences are
expected" tolerates the 299th, which might be the one that matters.

### 9.2 Metadata convergence first

Before writing any policy, every divergence was examined to see whether the
right fix was to make the metadata truthful. Where it was, it was.

| Divergence | Direction | Resolution |
|---|---|---|
| 3 column comments, 2 table comments | Database had them, models did not | Added to the models. **No database change** |
| 2 column comments | Both present, text had drifted apart | Models aligned to the applied SQL, which is authoritative |
| 1 column comment | Model had it, database did not | New comment-only migration `0043_schema_comment_convergence` |
| 1 nullability (`ioe.strategy_portfolio.total_future_option_value`) | Database `NOT NULL`, model optional | Model corrected — this was a real understatement of a constraint on a hash component |

Result: **zero** comment and nullability divergence remains. None of it needed a
policy entry, and the one migration touches no column, constraint, index,
trigger, policy, grant, or row.

CHECK constraints were deliberately *not* converged. Declaring them in
`__table_args__` would put two sources in a position to create the same
constraint, which the entry brief explicitly rules out.

### 9.3 The identity-keyed policy

`scripts/check_schema_drift.py` runs the comparison at its **widest** setting —
`compare_type=True`, `compare_server_default=True`, `include_schemas=True`, no
`include_object` filter, so indexes, foreign keys, unique constraints, CHECK
constraints, comments, and nullability are all compared. This is deliberately
wider than `migrations/env.py`, whose narrower lens exists so
`alembic revision --autogenerate` produces usable forward migrations.

Every resulting difference is reduced to an identity:

```
(kind, schema, table, object)
```

and matched against `db/schema_drift_policy.json`. Matched → a governed
divergence. Unmatched → **UNEXPECTED**, and the gate fails.

`add_table`, `add_column`, and `remove_column` are in `NEVER_GOVERNED`: they fail
even if someone adds a policy entry for them.

For `modify_type`, the recorded identity includes the **type pair**
(`TEXT->String`). Without it, a policy entry would bless any future type change
on that column; with it, `TEXT->Integer` is a different, unmatched difference.

Current governed inventory — by identity, not by count:

| Class | Entries | Why legitimate |
|---|---|---|
| `TYPE_AFFINITY` | 288 | PostgreSQL-specific type the ORM declares generically: `TEXT` for unbounded `String`, `CITEXT` for case-insensitive, `money_amt` domain for `Numeric` |
| `RAW_SQL_MANAGED_CHECK` | 179 | CHECK constraints created by `backend/db/sql` |
| `RAW_SQL_MANAGED_INDEX` | 163 | Indexes created by `backend/db/sql`, including partial, expression, and HNSW vector indexes |
| `SERVER_DEFAULT_OWNED_BY_SQL` | 156 | Server defaults declared in SQL; the ORM sets its Python-side default instead |
| `RAW_SQL_MANAGED_FK` | 107 | Foreign keys with `ON DELETE` actions or SQL-assigned names |
| `DB_ONLY_PARTITION_CHILD` | 9 | Declarative-partition children; the parent is mapped, the children are managed by partition DDL |
| `COLUMN_COMMENT_METADATA` | **0** | converged |
| `TABLE_COMMENT_METADATA` | **0** | converged |
| `NULLABILITY_METADATA` | **0** | converged |
| **Unexpected drift** | **0** | |

### 9.4 The gap the proof exposed, and how it was closed

The first run of the injection proof revealed a real hole. A raw-SQL index is
invisible to the ORM metadata, so its whole contribution to the comparison is
the single governed difference its policy entry names. **Drop the index and that
difference simply stops being produced** — nothing becomes "unexpected", and the
gate would have passed.

A policy entry that no longer matches anything is therefore not housekeeping: it
is the only signal that a governed object has vanished. The checker now treats a
vanished `RAW_SQL_MANAGED_*` or `DB_ONLY_PARTITION_CHILD` entry as a **failure**.
(`TYPE_AFFINITY` entries are excluded from that rule, since a column legitimately
removed in a migration takes its type entry with it and is already caught as
`remove_column`/`add_column`.)

### 9.5 Failure-injection proof

`./scripts/prove_schema_drift_gate.sh` — a disposable database at head, one
defect at a time, each reverted and re-verified:

```
== baseline ==
unexpected schema drift: 0
== injected defects ==
  ok  unexpected column          rejected: remove_column     ioe.scenario.drift_probe_col
  ok  missing column             rejected: add_column        ioe.scenario.note
  ok  changed type               rejected: modify_type       ioe.scenario.label (VARCHAR->Text)
  ok  unexpected nullable change rejected: modify_nullable   ioe.scenario.workflow_status (db=True model=False)
  ok  missing index              rejected: vanished remove_index ioe.scenario.ix_ioe_scenario_user
  ok  unexpected CHECK           rejected: remove_constraint ioe.scenario.ck_drift_probe
== governed divergences still pass ==
  ok  raw-SQL-owned CHECKs/indexes/FKs/types remain governed, not failures

schema drift gate proof: 7 passed, 0 failed
```

CHECK comparison is not globally suppressed. Comments are not globally
suppressed. Nullable differences are not globally suppressed.

### 9.6 Migration gates

- `scripts/check_migrations.sh` — empty database → `upgrade head` → table count
  assertion → `downgrade base` → assert no schema survives. Retained, including
  downgrade: the repository treats it as an invariant.
- `tests/unit/test_migration_hygiene.py` — every revision imports, revision ids
  are unique, exactly one base and **exactly one head**, no dangling
  `down_revision`, every revision reachable from the base, and every non-seed SQL
  file is referenced by some revision.
- `scripts/check_schema_drift_on_fresh_db.sh` — the drift gate against a database
  Alembic built from scratch, never a working database.

---

## 10. The release gate

`./scripts/release_gate.sh [--fast|--full]`

`--fast` (no database, ~1 minute): python runtime → dependency lock → Ruff →
protected mypy.

`--full` adds, in order: clean-install proof → gate failure-injection proof →
migration smoke → revision hygiene → schema drift on a fresh database → schema
drift injection proof → security invariants → security gate injection proof →
determinism → full fresh-database suite.

Nothing is swallowed. `set -euo pipefail` plus an explicit `run` wrapper means
the first failing command decides the exit code, and the summary names it.

### Non-blocking-escape audit

`grep -rn "|| true\|continue-on-error\|set +e\|allow_failure"` across
`backend/scripts`, `backend/tests`, and `.github`:

| Occurrence | Classification |
|---|---|
| `check_schema_drift_on_fresh_db.sh:18`, `prove_schema_drift_gate.sh:23`, `prove_security_gate.sh:25` | `EXIT`-trap cleanup dropping a disposable database. Not a check — tolerating a failed `DROP DATABASE IF EXISTS` on a database that may not exist |
| 3 further hits | Comments in `release_gate.sh` / `check_types.sh` / the workflow stating that `|| true` is *not* used |

No critical check is bypassed. Protected mypy, security tests, the full suite,
the migration gate, and the schema-drift gate are all blocking.

---

## 11. Gate failure-injection proof

`./scripts/prove_gates_fail.sh` — each gate is shown rejecting a real defect,
then the defect is reverted and the gate is shown going green again. Every
touched file is compared byte-for-byte against a pre-run snapshot at the end.

```
== baseline: every gate green before injection ==
  ok  ruff / protected mypy / dependency lock

== injected defects ==
  ok  ruff / lint             rejected: Found 13 errors.
  ok  protected mypy          rejected: engine.py:30 Incompatible return value type (got "int", …)
  ok  test suite              rejected: assert approx(r.federal_tax, 5675.37)
  ok  dependency lock         rejected: lock is stale
  ok  protected-scope policy  rejected: application packages outside the protected typing scope
  ok  migration hygiene       rejected: down_revision values with no matching revision

gate failure-injection proof: 6 passed, 0 failed
== every touched file must be byte-identical to its pre-run snapshot ==
  ok  no injected defect survived
```

Security invariants cannot be proved by editing a source file — they live in
PostgreSQL. `./scripts/prove_security_gate.sh` removes them from a disposable
database instead:

```
   suite identity: onyx_test (member of onyx_app_rw), provisioned by onyx_migrator
  ok  FORCE ROW LEVEL SECURITY   rejected: tables carrying user_id without ENABLE+FORCE row level security
  ok  ENABLE ROW LEVEL SECURITY  rejected: tables carrying user_id without ENABLE+FORCE row level security
  ok  PUBLIC EXECUTE revocation  rejected: PUBLIC can execute SECURITY DEFINER functions

security gate proof: 3 passed, 0 failed
```

No intentional failure remains in the final diff.

---

## 12. Pollution regression

Every other run in this repository starts from an empty database, which hides a
class of defect: a test that assumes its record is the only one, the first one,
or the newest one. Production is never empty.

`./scripts/pollution_regression.sh` builds a database, fills it by running the
whole suite once, then adds more with `scripts/seed_pollution.py` — six tenants,
all 17 freshness event types across two tax years and five jurisdiction values,
and outbox rows spread across every claim state. It then re-runs the suites most
exposed to accumulated state, **twice, in two different orders**, so "passes" is
distinguished from "passes when it runs first":

`test_ioe_freshness_events`, `test_freshness_producer_wiring` (Entry 9),
`test_integrity_scheduler_task`, `test_integrity_verification`,
`test_golden_replay`, `test_frozen_snapshot_execution` (Item 3A),
`test_frozen_scenario_baseline` (Item 3B), `test_scenario_integrity_closeout`.

**Decision: this is a release-level check, not per-commit.** It costs roughly
three full-suite runs (~12 minutes). CI runs the fresh-database suite on every
push; this runs before a release and after any change to freshness, replay, or
scheduling.

---

## 13. Limitations and remaining risks

**In this entry's scope, and honest about it:**

- `disallow_any_generics` is not enabled. 145 bare `dict`/`list` annotations
  remain in the calculation core. Tracked in the debt register.
- The schema-drift policy has 902 entries. That is data, not code, and it is
  identity-keyed — but a reviewer approving a regenerated policy has a large
  diff to read. Mitigated by the class breakdown and by `NEVER_GOVERNED`.
- The drift gate detects a *vanished* raw-SQL object via its stale policy entry.
  It cannot detect a raw-SQL index that was silently *redefined* (same name,
  different columns), because the ORM has nothing to compare against.
- No container image exists, so the runtime pin is enforced in `pyproject.toml`,
  `.python-version`, CI, and `check_python.sh`, but not in a Dockerfile.
- The CI workflow has not executed on GitHub — this repository had no `.github`
  directory before this change. Every step was run locally against a real
  PostgreSQL 16 with pgvector; the first push will be its first real execution.

**Outside this entry's scope** (unchanged, and not release-blocking for it):
rate/admission limiting, privacy and data-lifecycle implementation, backup/PITR
and a restore drill, production alerting infrastructure, operational ownership,
external tax review, external privacy/security review, and historical
executable-version retention.
