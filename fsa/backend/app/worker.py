"""Celery-приложение. Воркер запускается так:

    celery -A app.worker.celery_app worker --loglevel=info --concurrency=2
"""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "fsa",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Конкурентность OCR ограничена самим движком: он отвечает 429, когда
    # пул процессов занят, поэтому задачи ретраятся, а не копятся у него.
    task_acks_late=True,
    task_default_retry_delay=10,
)
