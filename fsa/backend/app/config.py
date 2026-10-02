"""Настройки сервиса. Значения по умолчанию совпадают с docker-compose,
поэтому приложение импортируется и тесты идут без внешней конфигурации."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        # Поле api_token читается из FSA_API_TOKEN, но должно оставаться
        # доступным по своему имени — иначе его не подменить в тестах.
        populate_by_name=True,
        # .env лежит рядом с backend/, чтобы CLI-скрипты и uvicorn читали
        # одну и ту же конфигурацию.
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
    )

    database_url: str = "postgresql+asyncpg://fsa:fsa@localhost:55434/fsa"

    minio_endpoint: str = "localhost:9110"
    minio_public_endpoint: str = "localhost:9110"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket_documents: str = "fsa-documents"
    presign_expiry_seconds: int = 900

    # Движок OCR выбирается здесь; все шесть говорят на одном контракте
    # (Nikolay_Stepankov/ocr/API_CONTRACT.md), поэтому смена движка не
    # требует изменений в коде.
    ocr_engine: str = "paddle"
    ocr_engine_host: str = "127.0.0.1"
    ocr_engine_url: str | None = None
    ocr_engine_key: str = ""
    ocr_request_timeout: float = 300.0

    max_upload_bytes: int = 52_428_800
    registry_max_upload_bytes: int = 1_073_741_824

    # Внутренний сервисный токен. Пустое значение оставляет /v1/* закрытым:
    # лучше отказать всем, чем открыть контур из-за незаполненной переменной.
    api_token: str | None = Field(default=None, validation_alias="FSA_API_TOKEN")
    phase3_auth_secret: str = ""
    phase3_auth_issuer: str = "supplier-document-automation"
    phase3_auth_audience: str = "supplier-document-automation-ui"

    # Соль для хэширования сервисных ключей. Пустое значение выключает их
    # выпуск и проверку — общий токен при этом продолжает работать, поэтому
    # контур не закрывается наглухо из-за незаполненной переменной.
    service_api_key_pepper: str = ""

    redis_url: str = "redis://localhost:6389/0"
    # Синхронный режим нужен тестам и разовым скриптам: задача выполняется
    # в вызывающем процессе, брокер не требуется.
    celery_task_always_eager: bool = False


settings = Settings()
