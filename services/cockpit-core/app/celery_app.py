from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "cockpit_core",
    broker=settings.redis_broker_url,
    backend=settings.redis_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.tz,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "morning_briefing": {
            "task": "cockpit.proactive_execution",
            "schedule": crontab(hour=7, minute=30),
            "args": ("Genera il piano operativo di oggi leggendo eventi e task non completati.",),
        },
        "midday_course_correction": {
            "task": "cockpit.proactive_execution",
            "schedule": crontab(hour=14, minute=0),
            "args": ("Verifica scostamenti dal piano mattutino e rialloca i blocchi di lavoro.",),
        },
    },
)

celery_app.autodiscover_tasks(["app.tasks"])
