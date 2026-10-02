"""Celery application. The worker container runs:
celery -A app.worker.celery_app worker
"""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "price_loader",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Шаг миграции 04: единый воркер обслуживает оба контура — задачи
    # прайса (app.tasks) и ордерного домена фазы 3 (extraction/bitrix/
    # pipeline; модули импортируются ниже, чтобы декораторы задач
    # привязались к этому приложению).
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "recover-stale-price-imports": {
            "task": "app.tasks.recover_stale_price_imports",
            "schedule": 300.0,
        },
        "dispatch-queued-rule-attempts": {
            "task": "app.tasks.dispatch_queued_rule_attempts",
            "schedule": float(settings.rule_generation_dispatch_interval_seconds),
        },
        "recover-stale-rule-attempts": {
            "task": "app.tasks.recover_stale_rule_attempts",
            "schedule": float(settings.rule_generation_recovery_interval_seconds),
        },
        # Шаг миграции 04: расписание outbox-диспетчеров фазы 3
        # (docs/migration/04-backend-bitrix.md — мерж воркера).
        "dispatch-certificate-extraction": {
            "task": "app.certificates.dispatch_outbox",
            "schedule": 2.0,
        },
        "dispatch-pipeline-runs": {
            "task": "app.pipeline.dispatch_outbox",
            "schedule": 2.0,
        },
        "dispatch-bitrix-jobs": {
            "task": "app.bitrix.dispatch_outbox",
            "schedule": 2.0,
        },
        # Отложенные повторы проверяются реже: минимальная задержка backoff —
        # секунды, и опрашивать таблицу чаще незачем.
        "dispatch-bitrix-retries": {
            "task": "app.bitrix.dispatch_retries",
            "schedule": 15.0,
        },
    },
)

# Import after app construction so task decorators bind to this Celery app.
# (worker.py фазы 3 делал то же; здесь привязываются задачи P3.)
from app.modules.extraction import tasks as extraction_tasks  # noqa: E402, F401
from app.modules.integrations.bitrix import tasks as bitrix_tasks  # noqa: E402, F401
from app.modules.pipeline import tasks as pipeline_tasks  # noqa: E402, F401
