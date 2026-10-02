"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware

from app import storage
from app.core.errors import register_exception_handlers
from app.core.middleware import CorrelationIdMiddleware
from app.api import (
    auth,
    profile,
    calculations,
    categories,
    corpus_runs,
    documents,
    files,
    health,
    matching,
    order_price_documents,
    orders,
    parse_rules,
    price_imports,
    price_items,
    price_versions,
    projects,
    reference,
    review,
    rule_proposals,
    service_clients,
    suppliers,
    versions,
)
from app.auth import ApiAuthMiddleware
from app.db import engine, SessionLocal
from app.modules.identity.bootstrap import ensure_initial_users
from app.modules.identity.permissions import PermissionsService

# Шаг миграции 03: роутеры ордерного домена фазы 3 (docs/migration/03-backend-domain.md).
from app.modules.catalogs.router import (
    catalogs_router,
    dictionary_router,
    synonyms_router,
)
from app.modules.certificates.router import router as certificates_router
from app.modules.checks.router import batches_router, items_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.documents.router import router as uploads_router
from app.modules.parameters.router import item_router as item_parameters_router
from app.modules.parameters.router import router as parameters_router
from app.modules.pipeline.bootstrap import ensure_pipeline_definition
from app.modules.reporting.router import artifacts_router
from app.modules.reporting.router import batch_router as batch_reports_router
from app.modules.reporting.router import item_router as item_reports_router
from app.modules.reporting.router import router as reports_router
from app.modules.rules.router import item_router as item_comparisons_router
from app.modules.rules.router import router as comparisons_router
from app.modules.verdicts.router import batch_router as batch_verdicts_router
from app.modules.verdicts.router import router as verdicts_router
# Шаг миграции 04: интеграция с Bitrix24 (docs/migration/04-backend-bitrix.md).
from app.modules.integrations.bitrix.router import router as bitrix_router

from app.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic (`alembic upgrade head` runs on container
    # start); the app no longer create_all's tables.
    try:
        storage.ensure_buckets()
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось создать бакеты MinIO")
    try:
        async with SessionLocal() as session:
            created_users = await ensure_initial_users(session, get_settings())
            # Шаг миграции 03: регистрация версии FSM конвейера (канон P3) —
            # без неё pipeline_runs не создаются вообще.
            registered_pipeline = await ensure_pipeline_definition(session)
            seeded_permissions = await PermissionsService(session).ensure_defaults()
            if created_users or registered_pipeline or seeded_permissions:
                await session.commit()
    except Exception:
        logger.exception("Identity bootstrap failed")
    yield
    await engine.dispose()


API_DESCRIPTION = """
Внутренний API загрузчика прайсов «DocPrice». Все пути начинаются с `/api/v1`.

## Как начать работать

1. `POST /api/v1/auth/login` с логином и паролем стенда — в ответе `access_token`.
2. Нажмите **Authorize** и вставьте этот токен (без слова `Bearer`). Он живёт
   ~15 минут; после истечения повторите шаг 1.
3. Вызывайте методы прямо отсюда через **Try it out**.

## Аутентификация

Единственная схема — `Authorization: Bearer <credential>`, и в этот заголовок
кладутся два разных вида учётных данных:

- **JWT браузерной сессии** из `POST /api/v1/auth/login` — весь API. Роль `user`
  видит только безопасные методы (`GET`/`HEAD`/`OPTIONS`), любые изменения
  требуют роли `admin`.
- **Сервисный ключ** вида `plk_<prefix>_<secret>` из `POST /api/v1/service-clients`
  — префиксы `/api/v1/price-imports`, `/api/v1/price-versions`,
  `/api/v1/price-items`, `/api/v1/review` и `/api/v1/catalog`, и только в
  пределах собственного client scope. Сервер различает вид учётных данных по
  формату значения, а не по отдельному заголовку.

Изменения черновика review (`PATCH /api/v1/review/{import_id}`) и действия
`validate`/`apply`/`activate-rule` сервисному ключу недоступны — они требуют
admin-JWT.

Публичные пути без токена: `GET /api/v1/health`, `POST /api/v1/auth/login`,
`POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`.

## Ресурсы

- `/price-imports` — задание импорта: создать, статус, замечания, результат.
- `/price-versions` — разобранные версии прайса: список, позиции, выгрузка,
  оригинал, предпросмотр, mapping, статус.
- `/price-items` — позиции поверх всех версий: поиск, категория, автоклассификация.
- `/review` — ручная проверка: очередь, черновик (`PATCH`), dry-run и применение.
- `/catalog` — общий классификатор и соответствия категориям поставщиков.
- `/parse-rules` — правила разбора и их жизненный цикл.
- `/reference` — валюты, словарь характеристик, профили LLM.

## Выбор проекта

Заголовок `X-Project-ID` выбирает рабочий проект. Если его не передать,
используется проект по умолчанию («Оригинальные прайсы»). Список — в
`GET /api/v1/projects`.
"""


app = FastAPI(
    title="DocPrice Price Loader",
    version="0.1.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
    # nginx на стенде проксирует на backend только `location /api/`, всё
    # остальное отдаётся как статика SPA. Документация под этим же префиксом
    # доступна снаружи без отдельного правила прокси.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "docExpansion": "none",
        "filter": True,
    },
)

app.add_middleware(ApiAuthMiddleware)

# Шаг миграции 02: обработчики ApiError (envelope фазы 3 / Problem Details фазы 2)
# и correlation-id — часть мержа core (docs/migration/01-backend-core.md).
register_exception_handlers(app)
app.add_middleware(CorrelationIdMiddleware)


def _openapi_with_security() -> dict:
    """Объявить аутентификацию в схеме — её держит middleware, не зависимости.

    `ApiAuthMiddleware` проверяет токен до маршрута, поэтому FastAPI ничего об
    этом не знает и генерирует схему без `security`. Клиент, собранный по такой
    схеме, не отправляет заголовок `Authorization` и получает 401 на каждом
    вызове. Публичные пути перечислены здесь ровно так же, как в middleware.

    Схема ровно одна: и браузерный JWT, и сервисный ключ приходят в
    `Authorization: Bearer` — их различает формат значения, а не транспорт.
    Отдельная `apiKey`-схема на `X-Api-Key` описывала заголовок, который сервер
    нигде не читает, поэтому сгенерированный по ней клиент получал 401.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "JWT из `POST /api/v1/auth/login` либо сервисный ключ "
                "`plk_<prefix>_<secret>` для путей `/api/v1/price-imports`."
            ),
        },
    }
    for path, operations in schema["paths"].items():
        if path in ApiAuthMiddleware.public_paths:
            continue
        for operation in operations.values():
            operation.setdefault("security", [{"bearerAuth": []}])
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_security


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(profile.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(suppliers.router, prefix="/api/v1")
app.include_router(versions.router, prefix="/api/v1")
app.include_router(reference.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(parse_rules.router, prefix="/api/v1")
app.include_router(price_imports.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(price_items.router, prefix="/api/v1")
app.include_router(price_versions.router, prefix="/api/v1")
app.include_router(categories.router, prefix="/api/v1")
app.include_router(rule_proposals.router, prefix="/api/v1")
app.include_router(service_clients.router, prefix="/api/v1")
app.include_router(corpus_runs.router, prefix="/api/v1")
app.include_router(matching.router, prefix="/api/v1")
app.include_router(orders.router, prefix="/api/v1")
app.include_router(calculations.router, prefix="/api/v1")
# Донор (merge-remote): паспорта/сертификаты с автопривязкой к канонам.
app.include_router(documents.router, prefix="/api/v1")

# Шаг миграции 03: ордерный домен фазы 3. Порядок повторяет main.py P3:
# identity → справочники → заявки → параметры/сверки → вердикты/отчёты.
# `/profile` подключается выше через app.api.profile (теперь реэкспорт модуля).
app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(batches_router, prefix="/api/v1")
app.include_router(items_router, prefix="/api/v1")
# Мостик Ф2→Ф3: прайс/счёт, загруженный менеджером со страницы заявки.
app.include_router(order_price_documents.router, prefix="/api/v1")
app.include_router(catalogs_router, prefix="/api/v1")
app.include_router(synonyms_router, prefix="/api/v1")
app.include_router(dictionary_router, prefix="/api/v1")
app.include_router(uploads_router, prefix="/api/v1")
app.include_router(certificates_router, prefix="/api/v1")
app.include_router(item_parameters_router, prefix="/api/v1")
app.include_router(parameters_router, prefix="/api/v1")
app.include_router(item_comparisons_router, prefix="/api/v1")
app.include_router(comparisons_router, prefix="/api/v1")
app.include_router(verdicts_router, prefix="/api/v1")
app.include_router(batch_verdicts_router, prefix="/api/v1")
app.include_router(item_reports_router, prefix="/api/v1")
app.include_router(batch_reports_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
# Шаг миграции 04: маршруты интеграции (503-гейт при bitrix_mode=disabled).
app.include_router(bitrix_router, prefix="/api/v1")
