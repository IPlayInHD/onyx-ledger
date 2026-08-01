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
    include=["workers.tasks.analysis", "workers.tasks.maintenance"],
)

celery_app.conf.task_routes = {
    "workers.tasks.analysis.*": {"queue": "analysis"},
    "workers.tasks.documents.*": {"queue": "documents"},
    "workers.tasks.ingestion.*": {"queue": "ingestion"},
    "workers.tasks.notify.*": {"queue": "notify"},
    "workers.tasks.maintenance.*": {"queue": "maintenance"},
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
    "yearly-legislation-import": {
        "task": "workers.tasks.maintenance.import_new_legislation",
        "schedule": crontab(month_of_year="1", day_of_month="2", hour="5", minute="0"),
    },
}
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1
