# Price Loader Backend

FastAPI backend основного контура DocPrice: identity, поставщики, загрузка и разбор прайс-листов, версии, review, parse rules, категории, matching и service clients.

## Компоненты

- FastAPI: `app.main:app`
- PostgreSQL и Alembic
- Redis и Celery worker
- MinIO для файлов
- deterministic-парсеры и опциональные LLM-предложения

Все API-маршруты начинаются с `/api/v1`. Swagger: `/api/docs`, health: `/api/v1/health`.

## Docker Compose

Из корня репозитория:

```powershell
docker-compose up -d --build price-api price-worker
docker-compose ps
```

`price-api` перед запуском автоматически выполняет `alembic upgrade head`, затем запускает Uvicorn. `price-worker` использует тот же image, но переопределяет команду и миграции не выполняет.

## Локальный запуск

Требуются Python 3.12, PostgreSQL, Redis и MinIO.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Worker:

```powershell
celery -A app.worker.celery_app worker --loglevel=info --concurrency=2
```

Ключевые настройки: `DATABASE_URL`, `REDIS_URL`, `MINIO_ENDPOINT`, `MINIO_PUBLIC_ENDPOINT`, `APP_AUTH_SECRET`, bootstrap-учётные записи `APP_*` и опциональные ключи LLM-провайдеров. Полный список: [`.env.example`](../.env.example).

## Миграции

```powershell
alembic current
alembic upgrade head
alembic history
```

Новая схема оформляется Alembic-миграцией. Приложение не вызывает `Base.metadata.create_all()` при старте.

## Тесты

```powershell
pytest
```

Corpus/evaluation-тесты могут пропускаться при отсутствии внешних наборов данных. Для семантических сценариев:

```powershell
pip install -r requirements-semantic.txt
```

## Parse rules

```powershell
docker-compose run --rm price-api python -m app.seed import /srv/seeds/parse_rules_builtin.json
python -m app.seed export
```

Импорт правил идемпотентен.

## Авторизация

Публичны только health и auth endpoints. Остальные запросы используют `Authorization: Bearer <credential>` — пользовательский JWT или service key. Подробности: [authentication.md](../docs/security/authentication.md).
