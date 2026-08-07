# mypy Debt Register

Companion to
[`production-engineering-quality-gate.md`](./production-engineering-quality-gate.md).

This register exists to be honest about what the type gate does **not** cover.
It is not a place to park new errors: a new error in a protected package fails
CI, and no entry here excuses one.

---

## 1. Modules excluded from the protected scope

**None.**

The protected scope is the package roots `app` and `workers` — the entire
shipped application, 188 source files, all at zero errors. There is no
production package outside it, so there is nothing to list here and no boundary
a defect could be moved across.

| Directory | In scope | Why |
|---|---|---|
| `app/**` | ✓ | the application |
| `workers/**` | ✓ | the Celery workers |
| `tests/**` | ✗ | test code. Not shipped. Assertions deliberately construct invalid states, and requiring full annotations on fixtures would make tests harder to read for no runtime benefit. **Ruff does cover it** (`ruff check app workers scripts tests`) |
| `migrations/**` | ✗ | Alembic revisions. Each is a ~10-line wrapper calling `apply_sql_file(...)`; the SQL is the substance and the schema-drift gate is what verifies it. Structure is covered by `tests/unit/test_migration_hygiene.py` |
| `scripts/**` | ✗ | operator tooling, not imported by the application. Covered by Ruff and exercised directly by the release gate |

`tests/unit/test_quality_gate_policy.py` asserts that any *new* top-level
importable package outside `tests`/`migrations`/`scripts` fails the build until
it is added to the protected scope.

---

## 2. Strictness not yet enabled

The gate is strict but not maximal. These are the remaining dials, each measured
against the current tree.

### P1 — production runtime, worth doing next

| Flag | Errors | Concentration | Risk of not having it | Remediation |
|---|---|---|---|---|
| `disallow_any_generics` | **145** in `app/services/ioe`, `app/services/tax_engine`, `app/database`, `workers`; **200** repo-wide across 51 files | Bare `dict` / `list` in signatures — heaviest in `domain/portfolio.py`, `orchestrator.py`, `replay/services.py`, `presentation.py` | **Moderate.** A bare `dict` return means the caller's element access is unchecked. It is how a canonical payload with a wrong value type reaches a hash function without complaint. It does *not* silence whole function bodies the way a missing annotation does | Mechanical: replace `dict` → `dict[str, Any]` (or a precise type where one exists) signature by signature. Best done as its own change so the diff is reviewable against hash-relevant code, not folded into feature work |

Deliberately deferred rather than rushed: 145 signature edits across
canonicalization, portfolio assembly, and replay would produce a large diff with
no behaviour change, in exactly the code where a review needs to be careful.

### P2 — support / admin paths

| Flag | Errors | Concentration | Assessment |
|---|---|---|---|
| `disallow_untyped_decorators` (globally) | **16** across 4 files | Entirely `@celery_app.task` and FastAPI route decorators | Blocked upstream, not by us. Celery ships no `py.typed` and has no stub package, so the decorator is `Any` and mypy reports every function it wraps as untyped regardless of its own annotations. Already scoped off in `workers.tasks.*` only. **Revisit when Celery ships typing or a `types-celery` stub exists** |

### P3 — style, not correctness

| Flag | Errors | Assessment |
|---|---|---|
| `no_implicit_reexport` | **234** across 45 files | These are `__init__.py` re-export conventions. Enabling it would require `as`-aliasing or explicit `__all__` on most package inits. A naming discipline, not a defect class. **Not planned** |

---

## 3. Third-party typing gaps

| Package | Gap | Blast radius | Handling |
|---|---|---|---|
| `celery` | No `py.typed`, no stub package | `bind=True` task `self`, `.delay()`, `.retry()`, `@celery_app.task` | `[[tool.mypy.overrides]] module = ["celery", "celery.*"] ignore_missing_imports = true`. The `self` parameter is annotated `Task` — honest about what it is, even though the name resolves to `Any`. **The task bodies are fully annotated and fully checked**, and every service they call is checked under the full configuration |
| `defusedxml` | Shipped without stubs | XML parsing in `app/services/tkms/parsers/xml_parser.py` | **Resolved.** `types-defusedxml` added to the dev extra and to the lock; no override needed |
| `structlog` | `get_logger` returns `Any` by design (the bound-logger type depends on `wrapper_class`) | `app/core/logging.py` | One narrow `cast(FilteringBoundLogger, …)` beside the `configure_logging` call that pins `wrapper_class`. Callers get a checked logger instead of an `Any` that would spread |
| `sqlalchemy` | `AsyncSession.scalar` is typed `-> Any`; `Result` has no `rowcount` | Repository and service read paths | Not suppressed. Every site binds the result to a **declared name** (`existing: Scenario | None = await session.scalar(...)`) so the method's declared return type survives, or uses one narrow `cast("CursorResult[Any]", …)` on the DML path |

None of these is `ignore_errors`. None removes a module from checking.

---

## 4. Live suppressions

Complete inventory across `app` and `workers`.

| Kind | Count | Where | Justification |
|---|---|---|---|
| `# type: ignore[misc]` | 2 | `app/services/ioe/portfolio/eligibility.py:147-148` | `node.pop("_parent")` / `node.pop("_version")` on a `TypedDict`. The two private keys are scaffolding for assembling the condition tree and are removed before the node is hashed. Both carry a specific error code and an adjacent comment. `warn_unused_ignores = true` fails the build if either stops being necessary |
| `cast("CursorResult[Any]", …)` | 4 | `freshness_relay.py:224`, `scenario/freshness_service.py:213,246,269` | `.rowcount` is on `CursorResult`, not the `Result` base. These are DML statements, which always return a `CursorResult` |
| `cast("Table", …)` | 1 | `freshness_events.py:123` | `__table__` is declared `FromClause` on the declarative base and is always a `Table`, which is what `pg_insert()` requires |
| `cast(FilteringBoundLogger, …)` | 1 | `core/logging.py:45` | see structlog above |
| `cast(Any, …)` | **0** | | |
| `# noqa` (added by this entry) | 0 | | |
| `ignore_errors` | **0** | | banned by `tests/unit/test_quality_gate_policy.py` |

---

## 5. Review triggers

Regardless of what CI says, re-read this register when:

1. **Celery ships typing, or `types-celery` appears** → remove both overrides
   and re-measure.
2. **A new top-level package is added** → the policy test fails; add it to
   `protected_scope` and fix its errors rather than widening the exclusion list.
3. **A `cast()` or `# type: ignore` is added** → it belongs in §4 with a
   justification, in the same change.
4. **`disallow_any_generics` work begins** → do it in the calculation core
   first (`app/services/ioe/domain`, `app/services/tax_engine`), as its own
   commit, and update §2.
