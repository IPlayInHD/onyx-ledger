"""Celery application — Redis broker + Beat schedule.

Workers run the async-heavy pipelines (analysis, OCR, ingestion, notifications)
off the API event loop, on independently-scalable queues.
"""
from __future__ import annotations

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
}

celery_app.conf.beat_schedule = {
    "daily-data-update-check": {
        "task": "workers.tasks.maintenance.check_data_updates",
        "schedule": crontab(hour="3", minute="0"),
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
    "yearly-legislation-import": {
        "task": "workers.tasks.maintenance.import_new_legislation",
        "schedule": crontab(month_of_year="1", day_of_month="2", hour="5", minute="0"),
    },
}
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1
