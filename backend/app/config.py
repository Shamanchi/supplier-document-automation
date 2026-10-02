"""Application settings loaded from environment variables.

Defaults match the docker-compose defaults so the app can be imported
(and unit tests run) without any environment configured.

Секции «Фаза 3» (uploads/OCR/Bitrix/SSE) влиты из supplier-document-automation
src/app/config.py по плану docs/migration/01-backend-core.md: общие поля
остаются на значениях фазы 2, переносятся только отсутствующие настройки.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str = "postgresql+asyncpg://price:price@localhost:5433/price_loader"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_public_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    # Шаг миграции 03: app/modules/documents/storage.py передаёт region_name
    # в boto3-клиент (канон P3), хотя MinIO регион игнорирует.
    minio_region: str = "us-east-1"
    minio_bucket_price_files: str = "price-files"
    minio_bucket_product_docs: str = "product-docs"

    presign_expiry_seconds: int = 900

    # Контур сертификатов (сервис FSA). Пустой адрес — контур выключен, и
    # подбор честно отвечает «не проверено» вместо выдумывания вердикта.
    # Эмбеддер подбора: "trigram" (без весов, работает везде) либо "mpnet" —
    # локальный paraphrase-multilingual-mpnet-base-v2 из архитектуры.
    embedding_provider: str = "trigram"
    # Дополнительная смысловая ступень каскада на локальной модели. Пусто —
    # ступень выключена; "mpnet" — paraphrase-multilingual-mpnet-base-v2.
    semantic_provider: str = ""

    fsa_base_url: str = ""
    fsa_api_token: str = ""

    # Two deployment-managed accounts are enough for the internal price-loader
    # UI. Passwords and the signing key are generated into .env/.env.prod by
    # run.sh; they are never embedded into the frontend image.
    app_admin_username: str = "admin"
    app_admin_password: str | None = None
    app_user_username: str = "user"
    app_user_password: str | None = None
    app_sysadmin_username: str = "sysadmin@example.local"
    app_sysadmin_password: str | None = None
    app_viewer_username: str = "viewer@example.local"
    app_viewer_password: str | None = None
    app_auth_secret: str | None = None
    phase3_auth_secret: str | None = None
    app_access_token_expiry_minutes: int = 15
    app_refresh_token_expiry_days: int = 7
    app_auth_secure_cookies: bool = False
    auth_secret: str | None = None
    access_token_expires_minutes: int = 15
    refresh_token_expires_days: int = 30

    # External price-import API. Keys are high-entropy bearer secrets; only a
    # keyed digest is stored. Keep the feature disabled until the additive
    # migration and a canary service client are provisioned.
    price_import_service_enabled: bool = False
    service_api_key_pepper: str | None = None
    price_import_stale_after_seconds: int = 3600
    price_import_webhook_timeout_seconds: int = 15

    # DEV-139 rollout guard. False preserves the legacy batch workflow; true
    # enables PostgreSQL-backed independent section tasks.
    durable_rule_generation_enabled: bool = False
    # Stage 6B is an independent rollout guard.  False preserves the exact
    # provider workflow even when Stage 6A classifies a section as deterministic.
    rule_generation_fast_path_enabled: bool = False
    # Dev-only deterministic rule-generation stub. True returns a schema-
    # conformant, deterministic proposal from local section evidence and never
    # calls an external provider, so a price file without a rule still reaches a
    # normal terminal state when no API key is configured. Production keeps the
    # flag off: inserting a real key restores the unchanged provider workflow.
    rule_generation_stub_mode: bool = False
    # A lease covers both provider HTTP attempts and the one semantic repair.
    # Heartbeats extend it while the owning section task is alive.
    rule_generation_lease_seconds: int = 900
    rule_generation_retry_delays_seconds: tuple[int, int, int] = (60, 300, 900)
    rule_generation_dispatch_limit: int = 100
    rule_generation_dispatch_interval_seconds: int = 30
    rule_generation_recovery_interval_seconds: int = 60

    # The key is intentionally server-only. Browser code never receives it;
    # an absent key keeps the model-proposal queue disabled and leaves the
    # deterministic/manual mapping path available.
    openai_api_key: str | None = None
    openai_api_base_url: str = "https://api.openai.com/v1"
    openai_rule_model: str = "gpt-4o"
    openai_rule_models: str = "gpt-4o"
    openai_rule_timeout_seconds: int = 75
    openai_rule_max_output_tokens: int = 3500
    # Large price lists are processed sequentially in bounded pages. The page
    # size controls provider pressure; the operation ceiling remains a final
    # budget guard and never silently drops sections.
    openai_rule_batch_page_size: int = 24
    openai_rule_max_sections_per_operation: int = 512
    # A per-version circuit breaker limits accidental repeated paid requests.
    # Production additionally needs authenticated per-user/supplier limits.
    openai_rule_start_cooldown_seconds: int = 300

    # Provider-neutral switch. ``openai`` keeps the existing deployment
    # compatible; ``yandexgpt`` uses Yandex Cloud's OpenAI-compatible API.
    llm_provider: str = "openai"
    yandexgpt_api_key: str | None = None
    yandexgpt_folder_id: str | None = None
    yandexgpt_api_base_url: str = "https://llm.api.cloud.yandex.net/v1"
    yandexgpt_rule_model: str = "qwen3-235b-a22b-fp8/latest"
    yandexgpt_rule_models: str = (
        "qwen3-235b-a22b-fp8/latest,qwen3.6-35b-a3b/latest,"
        "deepseek-v4-flash/latest,yandexgpt-5.1/latest"
    )

    # Direct DeepSeek OpenAI-compatible API. Secrets stay server-side; clients
    # can only select a model from ``deepseek_rule_models``.
    deepseek_api_key: str | None = None
    deepseek_api_base_url: str = "https://api.deepseek.com"
    deepseek_rule_model: str = "deepseek-v4-flash"
    deepseek_rule_models: str = "deepseek-v4-flash,deepseek-v4-pro"

    # Phoenix is enabled by default for this deployment.  It can still be
    # disabled explicitly; in that mode the OpenTelemetry SDK is never
    # imported or initialised and no exporter thread/network traffic exists.
    phoenix_enabled: bool = True
    phoenix_collector_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_project_name: str = "price-loader"
    phoenix_max_content_chars: int = 250_000
    phoenix_api_key: str | None = None

    # --- Фаза 3: организация-владелец данных (tender_refs.organization) ---
    # Понятия организации в схеме ещё нет; до его появления подставляется это
    # значение (предложение §2 IMPROVEMENT_PROPOSALS из фазы 3).
    organization_name: str = "ТД «DocPrice»"

    # --- Фаза 3: загрузка документов (docs/api/uploads) -------------------
    # Бакеты создаёт minio-init из docker-compose; приложение их не создаёт.
    upload_bucket: str = "documents"
    quarantine_bucket: str = "quarantine"
    #: Предел размера файла. 50 МБ покрывают ТЗ и паспорта; всё крупнее — повод
    #: включить resumable-загрузку, а не поднимать лимит.
    upload_max_file_bytes: int = 50 * 1024 * 1024
    #: Документов одной роли на позицию (uploads: DOCUMENT_LIMIT_EXCEEDED).
    upload_max_documents_per_role: int = 10
    #: Границы разбора OOXML: защита от zip bomb (uploads QA).
    upload_max_zip_entries: int = 2000
    upload_max_zip_uncompressed_bytes: int = 500 * 1024 * 1024

    # --- Фаза 3: извлечение ТХ --------------------------------------------
    # OCR локальный: документ не покидает инсталляцию.
    ocr_enabled: bool = True
    ocr_language: str = "rus+eng"
    ocr_dpi: int = Field(default=200, ge=72, le=600)
    ocr_timeout_seconds: int = Field(default=60, ge=1, le=600)
    ocr_min_confidence: float = Field(default=0.55, ge=0, le=1)
    extraction_min_confidence: float = Field(default=0.60, ge=0, le=1)

    # Внешний LLM только opt-in: до согласования data policy выключен.
    llm_extraction_enabled: bool = False
    llm_extraction_endpoint: str | None = None
    llm_extraction_api_key: str | None = None
    llm_extraction_model: str = "structured-extraction"
    llm_extraction_prompt_version: str = "certificate-v1"
    llm_extraction_timeout_seconds: int = Field(default=45, ge=1, le=600)
    llm_extraction_max_fragments: int = Field(default=50, ge=1, le=200)

    # --- Фаза 3: интеграция с Bitrix24 (docs/api/integrations/bitrix) -----
    #: Режим клиента портала:
    #:   `disabled` — интеграция выключена, все маршруты отвечают
    #:                503 BITRIX_NOT_CONFIGURED (значение по умолчанию);
    #:   `fake`     — встроенный стенд без сети, для демонстрации и тестов;
    #:   `webhook`  — реальный портал через входящий вебхук.
    #: Порт один и тот же: подключение реального портала — смена режима и
    #: заполнение `bitrix_base_url`/`bitrix_webhook_token`, а не правка кода.
    bitrix_mode: Literal["disabled", "fake", "webhook"] = "disabled"
    #: База вебхука вида https://portal.bitrix24.ru/rest/<user_id>/. Токен
    #: хранится отдельно и никогда не попадает ни в ответ API, ни в журнал.
    bitrix_base_url: str | None = None
    bitrix_webhook_token: str | None = None
    bitrix_timeout_seconds: int = Field(default=20, ge=1, le=120)
    #: Отдельный бюджет на перекачку файла. Метаданные и многомегабайтный
    #: документ не должны делить один таймаут: короткий режет закачку на
    #: медленном портале, длинный подвешивает воркер на зависшем чтении.
    bitrix_download_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    #: Поле сделки с номером тендера. Значение по умолчанию — предположение:
    #: штатного поля в crm.deal нет, фактическое имя даёт заказчик.
    bitrix_tender_field: str = "UF_CRM_TENDER_ID"
    #: Пользовательские поля сделки, в которых лежат файлы (ТЗ, паспорта).
    #: Пусто по умолчанию: угадывать имена UF-полей нельзя, их называет
    #: заказчик. Формат значения переменной окружения — JSON-массив.
    bitrix_file_fields: list[str] = Field(default_factory=list)
    #: Читать ли вложения комментариев таймлайна. Включено по умолчанию:
    #: почтовый контур Фазы 1 кладёт файлы письма именно туда.
    bitrix_read_timeline_files: bool = True
    #: Поле сделки с идентификатором папки Диска, если файлы лежат там.
    bitrix_deal_folder_field: str | None = None
    #: Папка Диска для отчётов. Без неё публикация кладёт файл в папку из
    #: `bitrix_deal_folder_field`, а при её отсутствии — отвергает запрос.
    bitrix_report_folder_id: str | None = None
    #: Попыток на задание, включая первую. Дальше — терминальный FAILED.
    bitrix_max_attempts: int = Field(default=5, ge=1, le=20)
    #: База backoff в секундах: задержка = base * 2^(attempt-1) + jitter.
    bitrix_retry_base_seconds: int = Field(default=5, ge=1, le=600)
    bitrix_retry_max_seconds: int = Field(default=15 * 60, ge=1, le=24 * 60 * 60)

    @property
    def bitrix_configured(self) -> bool:
        """Готова ли интеграция к работе.

        Режим `fake` считается настроенным намеренно: стенд не требует ни URL,
        ни токена, и должен вести себя как рабочий портал.
        """
        if self.bitrix_mode == "fake":
            return True
        return bool(
            self.bitrix_mode == "webhook"
            and self.bitrix_base_url
            and self.bitrix_webhook_token
        )

    # --- Фаза 3: SSE прогресса прогона (check-items §SSE) ------------------
    #: Как часто поток опрашивает журнал событий. Компромисс: заметно быстрее
    #: перечитывания позиции клиентом и не создаёт заметной нагрузки на базу.
    sse_poll_interval_seconds: float = Field(default=1.0, gt=0, le=10)
    #: Предел жизни подписки: прогон столько не идёт, а вечных соединений быть
    #: не должно. Клиент переподключается и догоняет по `Last-Event-ID`.
    sse_max_lifetime_seconds: int = Field(default=30 * 60, ge=1, le=6 * 60 * 60)

    # --- Фаза 3: bootstrap-аккаунты ----------------------------------------
    #: Создавать при первом старте, если таблица users пуста. Пароли по
    #: умолчанию совпадают с ролью — только для разработки.
    bootstrap_users_enabled: bool = True
    bootstrap_email_domain: str = "docprice.internal"
    #: Пароли dev-аккаунтов P3 вида <role>@<bootstrap_email_domain>. Заводятся
    #: только при явном задании (в config.py P3 дефолт — пароль, совпадающий
    #: с ролью; здесь осознанно None — решение шага 02: на живой БД нужны
    #: только развёртываемые аккаунты фазы 2 из APP_*_USERNAME/APP_*_PASSWORD).
    bootstrap_admin_password: str | None = None
    bootstrap_manager_password: str | None = None
    bootstrap_sysadmin_password: str | None = None
    bootstrap_viewer_password: str | None = None

    @property
    def sync_database_url(self) -> str:
        """URL для синхронных инструментов (Alembic): asyncpg → psycopg2."""
        return self.database_url.replace("+asyncpg", "+psycopg2")

    # --- Провайдеры LLM (прайс-контур фазы 2) ------------------------------

    @property
    def rule_model_api_key(self) -> str | None:
        if self.llm_provider.casefold() == "yandexgpt":
            return self.yandexgpt_api_key
        if self.llm_provider.casefold() == "deepseek":
            return self.deepseek_api_key
        return self.openai_api_key

    @property
    def rule_model_api_base_url(self) -> str:
        if self.llm_provider.casefold() == "yandexgpt":
            return self.yandexgpt_api_base_url
        if self.llm_provider.casefold() == "deepseek":
            return self.deepseek_api_base_url
        return self.openai_api_base_url

    @property
    def rule_model(self) -> str:
        if self.llm_provider.casefold() == "deepseek":
            return self.deepseek_rule_model
        if self.llm_provider.casefold() != "yandexgpt":
            return self.openai_rule_model
        model = self.yandexgpt_rule_model.strip()
        if model.startswith("gpt://"):
            return model
        if not self.yandexgpt_folder_id:
            return model
        return f"gpt://{self.yandexgpt_folder_id}/{model.lstrip('/')}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = Settings()
