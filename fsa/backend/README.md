# FSA backend — реестр сертификатов, OCR документов, сверка

Сервис делает три вещи:

1. держит зеркало реестра Росаккредитации в Postgres;
2. принимает пакеты сканов, распознаёт их и складывает в S3;
3. сверяет распознанный номер с реестром и выдаёт вердикт для расчёта.

Схема связки описана в [`app/fsa/README.md`](app/fsa/README.md).

## Запуск

```bash
cp ../.env.example ../.env                        # порты инфраструктуры
docker compose -f ../docker-compose.yml up -d     # postgres + minio + redis
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
cp .env.example .env                              # DATABASE_URL, MINIO_*, OCR_ENGINE_KEY, FSA_API_TOKEN
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8100
.venv/bin/celery -A app.worker.celery_app worker --loglevel=info --concurrency=2
```

Токен генерируется командой
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` и кладётся в
Общий Phase 3 JWT используется для пользовательского доступа к `/v1/*`.
незаполненной переменной нельзя.

## Аутентификация

Все маршруты `/v1/*` требуют `Authorization: Bearer <Phase 3 access JWT>`. `/health`
открыт для healthcheck — как в контракте OCR-движков. Учётных записей нет
намеренно: контур внутренний, токеном пользуются фронтенд и соседние сервисы.

```bash
curl -H "Authorization: Bearer $FSA_API_TOKEN" http://127.0.0.1:8100/v1/registry/stats
```

## Очередь

Celery + Redis, как в price_loader_app. Три задачи:

| Задача | Когда |
|---|---|
| `app.tasks.recognize_document` | загрузка с `background=true` — OCR уносится в очередь |
| `app.tasks.import_registry_file` | загрузка файла реестра |
| `app.tasks.recheck_documents` | массовая пересверка с реестром без повторного OCR |

Ретраи заложены: движок отвечает `429`, когда его пул занят, поэтому задача
переставляется, а не копится у него.

## Загрузка данных

```bash
# реестр: 54 076 карточек + 2.6 млн номеров, ~30 секунд
.venv/bin/python scripts/import_registry.py

# документы: S3 + OCR при отсутствии текстового слоя + сверка
.venv/bin/python scripts/import_documents.py --root "../../OCR_prices/Прайсы/прайсы"
```

Оба скрипта идемпотентны: реестр обновляется UPSERT-ом по номеру, документы
опознаются по sha256 и повторно через OCR не гоняются.

Файлы реестра можно добавлять и через интерфейс — `POST /v1/registry/imports`
принимает CSV карточек, список номеров и 7z-архив набора. Вид определяется по
содержимому файла, расширение служит подсказкой. Автоматической выкачки набора
нет намеренно: файлы кладут руками.

## API

| Маршрут | Назначение |
|---|---|
| `GET /health` | статус, выбранный движок, размер реестра |
| `GET /v1/registry/stats` | сколько карточек и сколько живых на дату |
| `GET /v1/registry/search?q=` | поиск по номеру, изготовителю, продукции |
| `GET /v1/registry/certificates/{номер}` | карточка; номер нормализуется |
| `GET /v1/registry/certificates/{номер}/successors` | вероятные замены |
| `POST /v1/documents/upload` | загрузить, распознать при необходимости, сверить |
| `GET /v1/documents` | список с фильтрами по вердикту, статусу, поставщику |
| `GET /v1/documents/summary` | сводка для дашборда |
| `GET /v1/documents/{id}` | карточка с текстом и ссылкой на оригинал |
| `POST /v1/documents/{id}/recheck` | пересверить без повторного OCR |
| `POST /v1/documents/recheck-all` | поставить в очередь пересверку всех документов |
| `POST /v1/registry/imports` | загрузить файл реестра (CSV, список номеров, 7z) |
| `GET /v1/registry/imports` | история загрузок со статусами |
| `GET /v1/registry/imports/{id}` | одна загрузка |

## Тесты

```bash
.venv/bin/python -m pytest -q
```

Движок в тестах замокан, база — SQLite в памяти с той же схемой.
