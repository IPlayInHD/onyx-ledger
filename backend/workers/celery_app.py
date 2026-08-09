"""Celery application — Redis broker + Beat schedule.

Workers run the async-heavy pipelines (analysis, OCR, ingestion, notifications)
off the API event loop, on independently-scalable queues.
"""
from __future__ import annotations

from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "onyx",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "workers.tasks.analysis",
        "workers.tasks.maintenance",
        "workers.tasks.privacy",
        "workers.tasks.tkms",
        "workers.tasks.ioe",
    ],
)

celery_app.conf.task_routes = {
    "workers.tasks.analysis.*": {"queue": "analysis"},
    "workers.tasks.documents.*": {"queue": "documents"},
    "workers.tasks.ingestion.*": {"queue": "ingestion"},
    "workers.tasks.notify.*": {"queue": "notify"},
    "workers.tasks.maintenance.*": {"queue": "maintenance"},
    # TKMS: one queue per stage so slow work never blocks and each scales alone
    "workers.tasks.tkms.parse": {"queue": "tkms_parse"},
    "workers.tasks.tkms.extract": {"queue": "tkms_extract"},
    "workers.tasks.tkms.promote": {"queue": "tkms_extract"},
    "workers.tasks.tkms.validate": {"queue": "tkms_validate"},
    "workers.tasks.tkms.compare": {"queue": "tkms_compare"},
    "workers.tasks.tkms.reindex": {"queue": "tkms_index"},
    # IOE: optimization is slow, freshness maintenance must never block it
    "workers.tasks.ioe.run_optimization": {"queue": "ioe"},
    "workers.tasks.ioe.invalidate_scenarios_for_analysis": {"queue": "ioe_freshness"},
    "workers.tasks.ioe.invalidate_scenarios_for_tax_year": {"queue": "ioe_freshness"},
    "workers.tasks.ioe.sweep_scenario_freshness": {"queue": "ioe_freshness"},
    "workers.tasks.ioe.relay_freshness_outbox": {"queue": "ioe_freshness"},
    # Verification replays sealed calculations, so it runs real engine work.
    # Its own queue keeps that off the freshness lanes and off optimization.
    "workers.tasks.ioe.verify_sealed_integrity": {"queue": "ioe_integrity"},
}

celery_app.conf.beat_schedule = {
    "daily-data-update-check": {
        "task": "workers.tasks.maintenance.check_data_updates",
        "schedule": crontab(hour="3", minute="0"),
    },
    # Bounds the growth of admission.rate_counter, whose cardinality is partly
    # attacker-controlled now that the login surface is throttled per claimed
    # identity. Hourly: the retention horizon is two hours, so a missed run has
    # room to be picked up by the next one.
    "admission-history-purge": {
        "task": "workers.tasks.maintenance.purge_admission_history",
        "schedule": crontab(minute="40"),
    },
    "monthly-analytics-roll": {
        "task": "workers.tasks.maintenance.roll_monthly_analytics",
        "schedule": crontab(day_of_month="1", hour="4", minute="0"),
    },
    # The NORMAL freshness path: drain the outbox frequently so an
    # invalidation reaches stored results within a minute of the change.
    "ioe-freshness-outbox-relay": {
        "task": "workers.tasks.ioe.relay_freshness_outbox",
        "schedule": crontab(minute="*"),
    },
    # Fallback sweep only: event-driven invalidation and read-time evaluation
    # are the primary freshness paths. Hourly so nothing lurks for long, bounded
    # per run so it can never become a full scan.
    "ioe-scenario-freshness-sweep": {
        "task": "workers.tasks.ioe.sweep_scenario_freshness",
        "schedule": crontab(minute="20"),
    },
    # Scheduled replay-integrity verification (closure entry 8C). The interval
    # is configuration-driven so it can be widened during an incident without a
    # deploy; `timedelta` rather than `crontab` because the value is an
    # arbitrary number of minutes and crontab cannot express one above 59.
    # Default 15 minutes at a default batch of 10 records. Measured batches are
    # far shorter than that, which makes overlap UNLIKELY — not impossible. A
    # slow database, a widened batch or a manual invocation can still put two
    # cycles in flight, so correctness under overlap comes from record-level
    # arbitration (the partial unique active-check index), never from timing.
    "ioe-integrity-verification": {
        "task": "workers.tasks.ioe.verify_sealed_integrity",
        "schedule": timedelta(minutes=settings.ioe_integrity_interval_minutes),
    },
    "yearly-legislation-import": {
        "task": "workers.tasks.maintenance.import_new_legislation",
        "schedule": crontab(month_of_year="1", day_of_month="2", hour="5", minute="0"),
    },
}
# `acks_late` + a prefetch of 1 is the backpressure baseline: a worker holds one
# message at a time, so a slow task cannot sit on a batch of others, and a
# crashed worker's message returns to the queue instead of being lost.
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1

# ---------------------------------------------------------------------------
# RESULT BACKEND PRIVACY (Entry 11A)
#
# Redis is both the broker and the result backend, so on FAILURE Celery writes
# the exception into Redis: `exc_type`, `exc_module`, and `exc_message` — the
# last being `str(exception)` verbatim.
#
# That is the leak. Every task re-raises through `self.retry(exc=exc)` or lets
# the original exception escape once retries are exhausted, and SQLAlchemy's
# `DBAPIError.__str__` renders as
#
#     (asyncpg...) ... [SQL: INSERT INTO finance.income_source ...]
#                      [parameters: ('...', '874321.19', 'Acme Payroll ...')]
#
# so a database error during a financial write puts the amount, the source name
# and the statement into a store with a one-day TTL and no tenant boundary.
# Verified by serializing a synthetic error through Celery's own
# `prepare_exception`; see tests/security/test_celery_result_privacy.py.
#
# Nothing in this repository reads a task result — there is no `AsyncResult`,
# no `.get()`, no `.ready()` anywhere — so the whole stored-result surface is
# cost without benefit. Turning it off removes the payload rather than trying to
# sanitize every exception that could ever reach the boundary.
#
# Failure information is NOT lost: structured logs carry closed error codes, and
# the TKMS pipeline dead-letters to `tkms.dead_letter` with a bounded payload.
# ---------------------------------------------------------------------------
celery_app.conf.task_ignore_result = True
# Explicit rather than relying on the default: with results ignored, this is the
# single setting that would put exception payloads back into Redis.
celery_app.conf.task_store_errors_even_if_ignored = False
# Also explicit. It was previously the framework default (1 day) — a reasonable
# bound arrived at by accident. Any task that opts back into results is capped
# here, and the number is now a decision someone can point at.
celery_app.conf.result_expires = timedelta(hours=24)

# ---------------------------------------------------------------------------
# Execution budgets (Entry 10).
#
# Without these a single pathological input runs forever, holding a worker slot,
# a database connection and — now — an admission lease. The admission lease
# expires on its own, but the worker slot does not, so the queue drains slower
# and slower while looking healthy.
#
# TWO limits per task, and the pair matters:
#   soft — raises SoftTimeLimitExceeded INSIDE the task, so the `async with`
#          blocks unwind, the transaction ROLLS BACK, and the admission lease is
#          released through the ordinary path.
#   hard — SIGKILLs a task that ignored the soft limit. Abrupt: no rollback runs
#          in-process, which is why it is set well above the soft limit and why
#          the database's own transaction abort, not application code, is what
#          guarantees no partial write survives.
#
# Global defaults are deliberately generous; the expensive classes are pinned
# individually below to the slowest run each has been measured at, with room.
# ---------------------------------------------------------------------------
celery_app.conf.task_soft_time_limit = 300
celery_app.conf.task_time_limit = 360

celery_app.conf.task_annotations = {
    # A full optimization: pinned snapshot, candidate normalization, scored
    # ranking, portfolio assembly with a bounded budget of up to 200 engine runs.
    "workers.tasks.ioe.run_optimization": {
        "soft_time_limit": 600, "time_limit": 660,
    },
    # Replay re-executes a sealed calculation; the scheduler already bounds each
    # RECORD, this bounds the batch.
    "workers.tasks.ioe.verify_sealed_integrity": {
        "soft_time_limit": 600, "time_limit": 660,
    },
    # Freshness work is small and frequent. A short budget here is protective:
    # a stuck relay is worse than a failed one, because the outbox keeps filling.
    "workers.tasks.ioe.relay_freshness_outbox": {
        "soft_time_limit": 120, "time_limit": 150,
    },
    "workers.tasks.ioe.sweep_scenario_freshness": {
        "soft_time_limit": 120, "time_limit": 150,
    },
    # Legislation ingestion parses whole documents.
    "workers.tasks.tkms.parse": {"soft_time_limit": 900, "time_limit": 960},
    "workers.tasks.tkms.extract": {"soft_time_limit": 900, "time_limit": 960},
    "workers.tasks.tkms.reindex": {"soft_time_limit": 1800, "time_limit": 1860},
}
