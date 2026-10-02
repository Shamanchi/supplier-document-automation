# API-модуль `integrations/bitrix`

## Назначение

Связывает Фазу 3 со сделкой Bitrix24: ищет сделку по `tenderId`, показывает доступные файлы, импортирует выбранные документы, публикует отчёты и timeline summary.

Интеграция асинхронна там, где операция может зависеть от сети или rate limits. Локальная проверка и отчёт остаются доступны при недоступности Bitrix24.

## Состояние реализации

**Актуализировано 2026-09-01.** Код: `backend/app/modules/integrations/bitrix/` (после миграции Фазы 3 в репозиторий Фазы 2; исходный путь — `supplier-document-automation/backend/src/app/modules/integrations/bitrix/`).

| Часть | Состояние |
|---|---|
| Маршруты контракта | реализованы все восемь |
| Порт портала (`port.py`) | нормализованные модели, raw payload наружу не выходит |
| Стенд (`fake.py`) | сделка, файлы, публикация, программируемые сбои |
| Клиент реального портала (`webhook.py`) | проверен на живом портале 2026-09-01: связь, поиск сделки, карточка, скачивание файла с Диска (446 КБ PDF), загрузка файла отчёта на Диск и запись сводки в таймлайн |
| Задания и повторы | `integration_jobs`, outbox, backoff, `Retry-After`, ручной retry |
| Связи (`external_links`) | заявка↔сделка, документ↔файл, артефакт↔файл, заявка↔комментарий |
| Контрактные тесты | `backend/tests/test_integrations_bitrix.py` |
| Экран заявок | импорт файлов и публикация отчёта реализованы (`frontend/src/widgets/order-detail/bitrix.tsx`), e2e — `frontend/tests/bitrix.spec.ts` |

Чего ещё нет: импорта из **лида**. Наблюдение на портале `gk-nx` (2026-09-01): вложения есть у 8 лидов «[Ручная обработка]» (17 файлов), а у сделок — ни одного. Пока почтовый контур Фазы 1 не начнёт прикладывать файлы к распознанным тендерам, импорт по номеру тендера найдёт пустой список: у лидов номера тендера нет, связать их с заявкой нечем.

## Режимы и конфигурация

Реализация порта выбирается настройкой `BITRIX_MODE`; прикладной код о выборе не знает, поэтому подключение портала — изменение конфигурации, а не правка кода.

| `BITRIX_MODE` | Поведение |
|---|---|
| `disabled` (по умолчанию) | все маршруты отвечают `503 BITRIX_NOT_CONFIGURED`, `health` — `status: DISABLED` |
| `fake` | встроенный стенд без сети: сделка `12345`, два файла; для демонстрации и тестов |
| `webhook` | реальный портал через входящий вебхук |

| Переменная | Назначение |
|---|---|
| `BITRIX_BASE_URL` | база вебхука вида `https://example.bitrix24.ru/rest/1/` |
| `BITRIX_WEBHOOK_TOKEN` | токен вебхука; наружу не отдаётся никогда |
| `BITRIX_TENDER_FIELD` | поле сделки с номером тендера (`ID` — если номер тендера и есть id сделки) |
| `BITRIX_FILE_FIELDS` | JSON-массив UF-полей сделки с файлами |
| `BITRIX_READ_TIMELINE_FILES` | читать вложения комментариев ленты сделки (по умолчанию `true`) |
| `BITRIX_DEAL_FOLDER_FIELD` | поле сделки с id папки Диска |
| `BITRIX_REPORT_FOLDER_ID` | папка Диска, куда кладётся отчёт |
| `BITRIX_TIMEOUT_SECONDS` | таймаут REST-вызова (по умолчанию 20 с) |
| `BITRIX_DOWNLOAD_TIMEOUT_SECONDS` | отдельный таймаут перекачки файла (по умолчанию 180 с) |
| `BITRIX_MAX_ATTEMPTS`, `BITRIX_RETRY_BASE_SECONDS`, `BITRIX_RETRY_MAX_SECONDS` | параметры повторов |

## Подключение портала: пошагово

Инструкция для того, кто настраивает интеграцию. Все проверочные вызовы нашего
API требуют Bearer-токена: `POST /api/v1/auth/login` → `accessToken`.

### Шаг 0. Сухой прогон на стенде

Прежде чем трогать портал, убедитесь, что контур поднят. В `.env` в корне
репозитория:

```
BITRIX_MODE=fake
```

```
docker compose up -d --force-recreate price-api price-worker price-beat
```

Проверка: `GET /api/v1/integrations/bitrix/health` отвечает
`{"configured": true, "mode": "fake", "status": "OK"}`, а
`GET /api/v1/integrations/bitrix/deals/12345` отдаёт карточку встроенной
сделки с двумя файлами. Если этот шаг не проходит, дело не в Bitrix.

### Шаг 1. Получить доступ к порталу

Bitrix24 → **Приложения → Разработчикам → Другое → Входящий вебхук**.
Права (scope): `crm`, `disk`, `user`. Сохранить.

Портал выдаст URL вида:

```
https://<портал>.bitrix24.ru/rest/<id_сотрудника>/<токен>/
```

Он делится на две настройки:

| Часть URL | Переменная |
|---|---|
| `https://<портал>.bitrix24.ru/rest/<id_сотрудника>/` | `BITRIX_BASE_URL` (слэш в конце необязателен) |
| `<токен>` | `BITRIX_WEBHOOK_TOKEN` |

Вебхук работает **от имени создавшего его сотрудника**: он обязан видеть нужные
сделки и папки Диска, иначе портал вернёт пустой результат, а не отказ прав.

Дальше методы портала удобно проверять прямо в браузере — Bitrix REST отвечает
и на GET: `https://<портал>.bitrix24.ru/rest/<id>/<токен>/<метод>.json`.

### Шаг 2. Найти, где лежит номер тендера

Штатного поля «номер тендера» в `crm.deal` нет, поэтому его имя задаётся
настройкой. Откройте `crm.deal.fields.json` и найдите поле, в котором отдел
ведёт номер:

- отдельное пользовательское поле → `BITRIX_TENDER_FIELD=UF_CRM_1700000000`;
- номер не ведут, работаем по идентификатору сделки → `BITRIX_TENDER_FIELD=ID`.

Проверка выбора:
`crm.deal.list.json?filter[UF_CRM_1700000000]=12345` должен вернуть ровно одну
сделку — **и у неё это поле должно быть равно искомому номеру**.

> Портал с фильтром по **несуществующему** полю молча его игнорирует и отдаёт
> первую попавшуюся сделку. Клиент это ловит и отвечает `BITRIX_CONTRACT_ERROR`
> вместо привязки заявки к чужой сделке, но опечатку в имени поля лучше
> заметить здесь.

### Шаг 3. Найти, где лежат файлы сделки

Поддержаны два источника, и хотя бы один должен быть настроен.

**UF-поля типа «файл».** Откройте `crm.deal.userfield.list.json`, возьмите
`FIELD_NAME` тех полей, у которых `USER_TYPE_ID` равен `file`:

```
BITRIX_FILE_FIELDS=["UF_CRM_1700000001","UF_CRM_1700000002"]
```

Значение — JSON-массив; иначе конфигурация не разберётся.

**Папка Диска, идентификатор которой лежит в поле сделки:**

```
BITRIX_DEAL_FOLDER_FIELD=UF_CRM_1700000003
```

Содержимое папки видно через `disk.folder.getchildren.json?id=<id>`.

**Вложения комментариев ленты** читаются по умолчанию (`BITRIX_READ_TIMELINE_FILES`)
и настройки не требуют — на этом проекте это основной путь: почтовый контур
Фазы 1 кладёт вложения письма в `crm.timeline.comment.add` массивом `FILES`
(`DocPrice_Agent-_postal_circuit-/app/modules/attachments.py`), за своим флагом
`BITRIX_ATTACH_ENABLED`. Портал отдаёт `FILES` **словарём** `{"<id>": {id, name, size, ...}}` — имя и
размер приходят прямо в комментарии, дополнительный запрос не нужен;
`disk.file.get` остаётся запасным путём для версий, отдающих голые
идентификаторы. Такие файлы приходят с `source: TIMELINE`.

**Если полей типа «файл» на портале нет вообще** (`crm.deal.userfield.list`
возвращает ноль записей с `USER_TYPE_ID = file`), то прикрепить файл к сделке
так, чтобы интеграция его увидела, невозможно — поле нужно завести. Через
интерфейс: **CRM → Настройки → Пользовательские поля → Сделка → Добавить
поле**, тип «Файл», включить множественность. Через REST (нужны права
администратора):

```
crm.deal.userfield.add
fields[FIELD_NAME]=UF_CRM_DEAL_FILES
fields[USER_TYPE_ID]=file
fields[MULTIPLE]=Y
fields[EDIT_FORM_LABEL][ru]=Файлы сделки
```

Роль документа поле не задаёт: `suggestedKind` выводится из имени файла, а
подтверждает менеджер. Поэтому одного множественного поля достаточно; два
отдельных («ТЗ» и «Поставщик») имеют смысл только как порядок в CRM.

Вариант с папкой Диска (`BITRIX_DEAL_FOLDER_FIELD`) требует, чтобы **в каждой
сделке** кто-то заполнил поле с идентификатором папки. На потоке это неудобно,
и оправдано лишь там, где такой процесс уже есть.

### Шаг 4. Выбрать папку для отчётов

`disk.storage.getlist.json` → взять нужное хранилище →
`disk.storage.getchildren.json?id=<storage>` → идентификатор папки:

```
BITRIX_REPORT_FOLDER_ID=1234
```

Без него публикация отвергается терминальной ошибкой **до** записи
комментария: сначала проверяется, есть ли куда положить файл.

### Шаг 5. Записать конфигурацию

`.env` в корне репозитория (он в `.gitignore`, токен в git не попадёт):

```
BITRIX_MODE=webhook
BITRIX_BASE_URL=https://example.bitrix24.ru/rest/1/
BITRIX_WEBHOOK_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
BITRIX_TENDER_FIELD=UF_CRM_1700000000
BITRIX_FILE_FIELDS=["UF_CRM_1700000001"]
BITRIX_DEAL_FOLDER_FIELD=
BITRIX_REPORT_FOLDER_ID=1234
```

Применить:

```
docker compose up -d --force-recreate price-api price-worker price-beat
```

Переменные раздаются якорем `x-backend-env`; `migrate` пересоздавать не нужно.

### Шаг 6. Проверить связь

1. `GET /api/v1/integrations/bitrix/health` — ожидается
   `{"configured": true, "mode": "webhook", "status": "OK"}`.
2. `GET /api/v1/integrations/bitrix/deals/<номер тендера>` — карточка сделки.
3. `GET /api/v1/integrations/bitrix/deals/<номер тендера>/files` — список файлов
   с предложенной ролью.

### Шаг 7. Первый импорт

```
POST /api/v1/integrations/bitrix/deals/<номер>/imports
Idempotency-Key: <uuid>

{
  "checkBatchId": "<uuid заявки>",
  "files": [
    {"externalFileId": "<из шага 6>", "kind": "REQUIREMENT",
     "checkItemId": "<uuid позиции>", "role": "REQUIREMENT"},
    {"externalFileId": "<ценовой из шага 6>", "kind": "PRICE",
     "priceDocumentType": "SUPPLIER_QUOTE", "supplierId": "<uuid поставщика>"}
  ]
}
```

Ответ `202` с `jobId`; состояние — `GET /api/v1/integrations/bitrix/imports/{jobId}`
до `COMPLETED`. Успешный импорт заодно связывает заявку со сделкой — без этой
связи публикация невозможна.

### Шаг 8. Первая публикация

Предусловия: вердикт заявки утверждён, сводный XLSX собран, шаг 7 выполнен.

```
POST /api/v1/integrations/bitrix/check-batches/<uuid заявки>/publish
Idempotency-Key: <uuid>

{"reportArtifactIds": ["<uuid артефакта>"], "addTimelineComment": true}
```

Состояние — `GET /api/v1/integrations/bitrix/publications/{jobId}`.

### Диагностика

| Симптом | Причина |
|---|---|
| `health`: `configured: false`, `status: DISABLED` | `BITRIX_MODE` не `webhook`, либо не заданы URL/токен |
| `health`: `errorCode: BITRIX_FAILED_AUTH` | токен неверен, отозван или у сотрудника нет нужных прав |
| `health`: `errorCode: BITRIX_NETWORK_ERROR` | портал недоступен из контейнера (сеть, прокси, DNS) |
| задание с `BITRIX_TIMEOUT` на шаге `download` | портал не отдал файл за отведённое время; повтор произойдёт сам, при регулярных срабатываниях поднять `BITRIX_DOWNLOAD_TIMEOUT_SECONDS` |
| `404 BITRIX_DEAL_NOT_FOUND` при верном номере | неверный `BITRIX_TENDER_FIELD` |
| `BITRIX_CONTRACT_ERROR` «найдено сделок: N» | в портале дубль: две сделки с одним номером тендера |
| `BITRIX_CONTRACT_ERROR` «портал вернул сделку, у которой … не равен …» | поля из `BITRIX_TENDER_FIELD` на портале нет — фильтр проигнорирован |
| список файлов пуст | сотрудник вебхука не видит файлы, либо указано не то UF-поле |
| «Источник файлов сделки не настроен» | не заданы ни `BITRIX_FILE_FIELDS`, ни `BITRIX_DEAL_FOLDER_FIELD` |
| «Папка Диска для отчётов не настроена» | не задан `BITRIX_REPORT_FOLDER_ID` |
| задание в `RETRY_SCHEDULED` с `BITRIX_RATE_LIMITED` | портал ограничивает частоту (штатно ~2 запроса/с); повтор произойдёт сам |
| `422 BITRIX_BATCH_NOT_LINKED` | заявка не связана со сделкой — сначала импорт (шаг 7) |

Задание, упавшее по повторяемой причине, возвращается в очередь само
(beat-задача `app.bitrix.dispatch_retries`). Ручной повтор —
`POST /api/v1/integrations/bitrix/publications/{jobId}/retry`; терминальные
ошибки (`BITRIX_FAILED_AUTH`, отказ проверок загрузки) повтору не подлежат и
отвечают `409 BITRIX_JOB_NOT_RETRYABLE`.

## Маршруты

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/v1/integrations/bitrix/health` | состояние конфигурации/последней проверки связи, без секретов |
| `GET` | `/api/v1/integrations/bitrix/deals/{tender_id}` | найти и нормализовать карточку сделки |
| `GET` | `/api/v1/integrations/bitrix/deals/{tender_id}/files` | список доступных файлов сделки/диска |
| `POST` | `/api/v1/integrations/bitrix/deals/{tender_id}/prepare` | идемпотентно создать/связать локальную заявку и вернуть комплект файлов |
| `POST` | `/api/v1/integrations/bitrix/deals/{tender_id}/imports` | импортировать выбранные файлы в Check batch/item |
| `GET` | `/api/v1/integrations/bitrix/imports/{job_id}` | состояние импорта |
| `GET` | `/api/v1/integrations/bitrix/check-batches/{batch_id}/status` | статус комплекта заявки (локальные данные) |
| `POST` | `/api/v1/integrations/bitrix/check-batches/{batch_id}/publish` | опубликовать отчёт и summary |
| `GET` | `/api/v1/integrations/bitrix/publications/{job_id}` | состояние публикации |
| `POST` | `/api/v1/integrations/bitrix/publications/{job_id}/retry` | ручной retry terminal/recoverable job по policy |

`imports/{job_id}` и `publications/{job_id}` отдают одну и ту же модель задания; маршрут retry принимает задание любого типа — состояние всех внешних операций живёт в одной таблице `integration_jobs`.

### Подготовка заявки из сделки

`POST /api/v1/integrations/bitrix/deals/{tender_id}/prepare` требует
`Idempotency-Key` и выполняет только локальную подготовку заявки: находит
сделку, создаёт или переиспользует `TenderRef` и `CheckBatch`, затем возвращает
нормализованную карточку сделки и доступные файлы. Файлы не скачиваются и не
становятся документами до отдельного подтверждения менеджера.

Если передан `checkBatchId`, используется существующий пакет. Если он не
передан, сервер переиспользует пакет с тем же тендером либо создаёт новый из
`items`; для нового пакета `items` обязателен. Повторный запрос с тем же
`tenderId` не создаёт второй `TenderRef` или `CheckBatch`.

Пример запроса:

```json
{
  "items": [
    {"positionNo": 1, "name": "Насос Н1", "supplier": null}
  ]
}
```

Ответ содержит `checkBatchId`, `tenderRefId`, `status: "FILES_AVAILABLE"`,
карточку `deal` и массив `files` с теми же подсказками роли, что и
`GET /deals/{tender_id}/files`.

## Health

```json
{
  "configured": true,
  "mode": "webhook",
  "status": "OK",
  "checkedAt": "2026-09-01T09:45:00Z",
  "errorCode": null
}
```

`status`: `OK` — связь установлена, `ERROR` — портал ответил сбоем (`errorCode` содержит класс сбоя), `DISABLED` — интеграция выключена. URL портала и токен в ответе не появляются.

## Карточка сделки

```json
{
  "tenderId": "12345",
  "bitrixDealId": "9876",
  "title": "Поставка насосного оборудования",
  "customer": "АО Заказчик",
  "stage": "PREPARATION",
  "assignee": "Иван Петров",
  "updatedAt": "2026-06-30T09:45:00Z",
  "externalUrl": "https://.../crm/deal/details/9876/"
}
```

Response является внутренней нормализованной моделью и не должен протекать raw Bitrix payload во Frontend.

## Список файлов

```json
{
  "items": [
    {
      "externalFileId": "disk-file-id",
      "name": "Техническое задание.xlsx",
      "sizeBytes": 34291,
      "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "source": "DEAL_FIELD",
      "createdAt": "2026-06-29T12:00:00Z",
      "suggestedKind": "REQUIREMENT",
      "kindConfidence": "85.00",
      "alreadyImported": false,
      "suggestedRole": "REQUIREMENT",
      "roleConfidence": "85.00",
      "alreadyImportedDocumentId": null
    }
  ],
  "notes": [
    "Вложения письма: 1 файл(ов). Не приложены (превышен размер): Каталог.pdf"
  ]
}
```

`notes` — тексты комментариев ленты сделки (до пяти). Возвращаются не для
полноты картины: Фаза 1 не грузит на портал вложения крупнее своего лимита
(`bitrix_inline_mb`) и перечисляет их имена в тексте. Без этого менеджер видел
бы короткий список файлов и не знал, что часть документов до портала не
доехала. Экран показывает `notes` над таблицей файлов.

`suggestedKind` — только предложение по имени файла (`service.suggest_kind`):
`REQUIREMENT` (ТЗ), `FACT` (техдокумент) или `PRICE` (прайс, счёт, КП). Прайс
никогда не определяется по расширению. Менеджер подтверждает тип, роль и
позицию; `suggestedRole`/`roleConfidence` — тот же тип, если он укладывается в
роль позиции (`REQUIREMENT`/`FACT`), для `PRICE` роль пуста. `alreadyImported`
и `alreadyImportedDocumentId` заполняются по `external_links`, поэтому
повторный импорт виден до отправки запроса; `alreadyImportedPriceFileId`
указывает на ценовой контур Ф2.

`source`: `DEAL_FIELD` — UF-поле сделки, `DISK` — папка Диска, `TIMELINE` — вложение таймлайна.

## Импорт

```json
{
  "checkBatchId": "uuid",
  "files": [
    {
      "externalFileId": "disk-file-id",
      "kind": "REQUIREMENT",
      "checkItemId": "uuid",
      "role": "REQUIREMENT"
    },
    {
      "externalFileId": "price-file-id",
      "kind": "PRICE",
      "priceDocumentType": "SUPPLIER_QUOTE",
      "supplierId": "uuid"
    }
  ]
}
```

`kind` подтверждает менеджер. Для Ф3 допустимы `REQUIREMENT`/`FACT` —
обязательны `checkItemId` и `role`; старый запрос без `kind` трактуется как
Ф3 по роли. Для `PRICE` обязательны `supplierId` и `priceDocumentType`:
`PRICE_LIST` — общий прайс поставщика (после успешного разбора может стать
активным), `SUPPLIER_QUOTE` — разовый счёт/КП, который действует только в
своей заявке и не попадает в общий активный каталог.

`Idempotency-Key` обязателен. Response `202`:

```json
{
  "jobId": "uuid",
  "status": "QUEUED",
  "filesTotal": 1
}
```

Проверки выполняются синхронно, до постановки задания: файл существует в сделке, позиция принадлежит указанной заявке, файл не импортирован ранее. Успешный запрос дополнительно связывает заявку со сделкой (`external_links`) и заполняет `tender_refs` — без этой связи публикация отчёта невозможна.

Worker скачивает файл, применяет тот же validation/quarantine contract, что `/uploads`, и сохраняет документ с `documents.source = 'BITRIX'`. Файл, отвергнутый проверками загрузки, переводит задание в `FAILED` без повторов: результат от повтора не изменится.

Ценовой файл проходит тот же контур Phase 2: `PriceFile` → `PriceVersion` →
`PriceImport` (`price_files.source = 'BITRIX'`), проверку реальной сигнатуры и
дедупликацию по `external_links` и `(serviceClientId, idempotencyKey)`. Разбор
запускается штатной задачей `app.tasks.run_price_import` после коммита
задания. `SUPPLIER_QUOTE` сохраняет связь с заявкой и участвует в её
подборе уже в статусе `validated`; активация такого документа как общего
прайса запрещена (`409`), чтобы он не вытеснил настоящий прайс поставщика.

## Статус комплекта заявки

`GET /api/v1/integrations/bitrix/check-batches/{batch_id}/status` отвечает
состоянием комплекта заявки, собранным **только из локальных данных**:
экран не должен зависеть от доступности Bitrix24. Источники — `external_links`
(связь заявки со сделкой и признак показанного комплекта), документы позиций
заявки, `price_versions`, `price_imports` и `integration_jobs`.

```json
{
  "checkBatchId": "uuid",
  "status": "PRICE_IMPORT_RUNNING",
  "dealLinked": true,
  "dealId": "9876",
  "tenderId": "12345",
  "filesAvailable": true,
  "filesCount": 3,
  "documentsImported": 2,
  "priceDocuments": 2,
  "priceReady": 1,
  "priceRunning": 1,
  "priceAttention": 0
}
```

Статусы идут по возрастанию готовности:

| Статус | Когда выставляется |
|---|---|
| `BITRIX_NOT_LINKED` | заявка не связана со сделкой (`external_links`) |
| `BITRIX_DEAL_FOUND` | сделка связана, комплект файлов ещё не показывали |
| `FILES_AVAILABLE` | `prepare` показал комплект файлов сделки |
| `DOCUMENTS_IMPORTED` | импортированы документы Ф3, ценовые документы в комплекте есть, но ещё не готовы |
| `PRICE_IMPORT_RUNNING` | хотя бы один `PriceImport` заявки в работе |
| `PRICE_REVIEW_REQUIRED` | `PriceImport` требует человека: `review_required`, `needs_ocr` или `failed` |
| `PRICE_READY` | ценовой документ комплекта готов, документы Ф3 ещё нет |
| `READY_FOR_CALCULATION` | документы Ф3 импортированы, а ценовые документы комплекта либо готовы, либо отсутствуют (расчёт может опираться на локальный каталог) |

К заявке относятся: `SUPPLIER_QUOTE` c `price_versions.check_batch_id = batch_id`
и `PRICE_LIST`, импортированный заданием этой заявки (определяется по
`integration_jobs.request_meta.files` и `external_links`). Блокирующие
состояния (`PRICE_IMPORT_RUNNING`, `PRICE_REVIEW_REQUIRED`) имеют приоритет над
частичной готовностью. Поля `dealId`, `tenderId`, `filesCount` могут быть
`null`; счётчики при отсутствии данных равны нулю.

## Публикация

```json
{
  "reportArtifactIds": ["uuid", "uuid"],
  "addTimelineComment": true,
  "includeKeyMismatches": true
}
```

Preconditions: вердикт заявки утверждён (`managerVerdict != PENDING` и есть `approvedAt`), артефакты принадлежат готовому snapshot этой заявки и ещё не опубликованы, заявка связана со сделкой. `Idempotency-Key` гарантирует отсутствие повторного файла/comment при retry.

Response `202`:

```json
{
  "jobId": "uuid",
  "status": "QUEUED",
  "tenderId": "12345"
}
```

Текст комментария собирается из snapshot отчёта (`summary.build_comment`): вердикт, число позиций, число расхождений и до десяти ключевых — обязательные раньше необязательных. При `includeKeyMismatches: false` перечень опускается, счётчик остаётся.

## Job model

```json
{
  "id": "uuid",
  "type": "PUBLISH_REPORT",
  "status": "RETRY_SCHEDULED",
  "attempt": 2,
  "nextRetryAt": "2026-06-30T11:05:00Z",
  "result": null,
  "error": {"code": "BITRIX_RATE_LIMITED", "message": "…", "retryable": true}
}
```

Состояния: `QUEUED`, `RUNNING`, `RETRY_SCHEDULED`, `COMPLETED`, `FAILED`, `CANCELLED`.

`result` импорта — `{"documents": {"<externalFileId>": "<documentId>"},
"prices": {"<externalFileId>": {"priceFileId": "...", "priceVersionId": "...",
"priceImportId": "...", "status": "...", "supplierId": "...", "supplierName": "...",
"documentType": "SUPPLIER_QUOTE", "checkBatchId": "...", "fileName": "...",
"statusUrl": "/api/v1/price-imports/..."}}}`; публикации — `{"dealId": "...", "files": {"<artifactId>": "<bitrixFileId>"}, "commentId": "..."}`.

## Retry policy

| Ошибка | Класс (`error.code`) | Поведение |
|---|---|---|
| timeout | `BITRIX_TIMEOUT` | exponential backoff + jitter |
| сеть недоступна | `BITRIX_NETWORK_ERROR` | exponential backoff + jitter |
| 5xx | `BITRIX_SERVER_ERROR` | exponential backoff + jitter |
| 429 | `BITRIX_RATE_LIMITED` | задержка из `Retry-After`, без наложения собственного backoff |
| 401/403 | `BITRIX_FAILED_AUTH` | терминально, без повторов |
| deal/file 404 | `BITRIX_DEAL_NOT_FOUND`, `BITRIX_FILE_NOT_FOUND` | терминальная бизнес-ошибка |
| invalid mapping/payload, отказ проверок загрузки | `BITRIX_CONTRACT_ERROR` | терминальная contract-ошибка |

Задержка — `BITRIX_RETRY_BASE_SECONDS * 2^(attempt-1)` с ограничением сверху и jitter. После `BITRIX_MAX_ATTEMPTS` задание переходит в `FAILED` с `error.reason = ATTEMPTS_EXHAUSTED`. Повторы Celery намеренно не используются: политика знает про `Retry-After` и различает повторяемые и терминальные ошибки, чего механизм очереди не умеет.

Отложенный повтор возвращает в очередь beat-задача `app.bitrix.dispatch_retries` по `next_retry_at`; первичная постановка идёт через transactional outbox (`app.bitrix.dispatch_outbox`).

## Result публикации

После успеха сохраняются Bitrix file IDs, timeline comment ID, deal ID и timestamps — в `integration_jobs.result` и в `external_links`. Локальный ReportArtifact не заменяется внешней ссылкой и остаётся доступен даже после провала публикации.

Повторная публикация другого артефакта той же заявки **не создаёт второй комментарий**: наличие связи `check_batch ↔ timeline_comment` и есть признак того, что сводка в сделке уже стоит.

## Безопасность

- webhook/OAuth credentials не возвращаются через API;
- health endpoint не раскрывает URL с token;
- raw provider payload не сохраняется: наружу и в БД идут только нормализованные модели;
- Frontend не обращается к Bitrix24 напрямую;
- импорт доступен ролям с правом записи проверок (`manager`, `admin`, `sysadmin`);
- все операции имеют correlation ID и audit event.

## Ошибки API

- `404 BITRIX_DEAL_NOT_FOUND`;
- `404 BITRIX_FILE_NOT_FOUND`;
- `404 BITRIX_JOB_NOT_FOUND`;
- `409 FILE_ALREADY_IMPORTED`;
- `409 REPORT_ALREADY_PUBLISHED`;
- `409 BITRIX_JOB_NOT_RETRYABLE` — задание завершено успешно либо упало по неустранимой причине;
- `422 IDEMPOTENCY_KEY_CONFLICT`;
- `422 BITRIX_FILE_MAPPING_REQUIRED` — нет типа/позиции либо для `PRICE` не указан поставщик;
- `404 BITRIX_SUPPLIER_NOT_FOUND` — выбранный поставщик ценового файла не найден;
- `422 BITRIX_PRICE_DOCUMENT_TYPE_REQUIRED` — не указано назначение ценового документа;
- `422 BITRIX_BATCH_NOT_LINKED` — заявка не связана со сделкой, публиковать некуда;
- `422 REPORT_NOT_READY`;
- `428 IDEMPOTENCY_KEY_REQUIRED`;
- `502 BITRIX_FAILED_AUTH` / `503 BITRIX_RATE_LIMITED` — сбой портала на синхронном чтении;
- `503 BITRIX_NOT_CONFIGURED`.

`BITRIX_BATCH_NOT_LINKED` и `BITRIX_JOB_NOT_RETRYABLE` добавлены к исходной таблице контракта: предусловие «batch связан со сделкой» и правило повтора были описаны словами, но кода ошибки не имели.

## Что требуется от заказчика

Без этих ответов режим `webhook` включать нельзя — сопоставление полей не угадывается:

1. Доступ: URL портала и входящий вебхук (или OAuth-приложение) с правами `crm`, `disk`, `user`.
2. Где лежит номер тендера: UF-поле сделки, `TITLE` или сам `ID` (`BITRIX_TENDER_FIELD`).
3. Откуда брать файлы: UF-поля сделки, папка Диска или вложения таймлайна (`BITRIX_FILE_FIELDS`, `BITRIX_DEAL_FOLDER_FIELD`).
4. Куда класть отчёт: папка Диска, UF-поле сделки или только комментарий (`BITRIX_REPORT_FOLDER_ID`).
5. Тестовая сделка sandbox с реальными файлами — без неё проверки приёмки получают статус `BLOCKED_BY_CUSTOMER_INPUT`.
6. Нужно ли двигать стадию сделки по результату проверки.

## Экран Orders (frontend)

Реализовано в `frontend/src/widgets/order-detail/bitrix.tsx`.

| Действие на экране | Где | Эндпоинты |
|---|---|---|
| «Загрузить из B24» | шапка карточки заявки | `GET /deals/{tender}`, `GET /deals/{tender}/files`, `POST /deals/{tender}/imports`, `GET /imports/{jobId}` |
| «Отправить в B24» | панель вердикта заявки, рядом с артефактами отчёта | `POST /check-batches/{id}/publish`, `GET /publications/{jobId}`, `POST /publications/{jobId}/retry` |

Правила экрана:

- оба действия скрыты, пока `GET /health` не ответил `configured: true` —
  кнопка, которая гарантированно вернёт `503`, вводит в заблуждение;
- импорт скрыт у заявки без `tenderId`: связывать не с чем;
- тип, роль и позицию для каждого файла выбирает менеджер, `suggestedKind`
  только подставляется в селектор по умолчанию;
- уже импортированный файл виден, но недоступен для выбора;
- состояние задания показывается строкой со статусом, номером попытки и
  причиной отказа; кнопка «Повторить» появляется только для повторяемых ошибок;
- предусловия публикации проверяет backend (вердикт утверждён, отчёт готов,
  заявка связана со сделкой), экран показывает его сообщение как есть.

## QA/contract tests

Реализованы в `backend/tests/test_integrations_bitrix.py` против стенда `FakeBitrixClient` — той же реализации порта, что работает в режиме `bitrix_mode=fake`:

- fake Bitrix: deal/files happy path;
- timeout, 429 с Retry-After, 401/403 и 404;
- duplicate import/publish;
- retry не создаёт второй timeline comment;
- import применяет upload security checks;
- локальный отчёт доступен при failed publish;
- credentials отсутствуют в response/log/audit;
- исчерпание попыток, повторная доставка завершённого задания, отбор заданий по `next_retry_at`.

Часть тестов требует PostgreSQL: без `TEST_DATABASE_URL` они пропускаются, проверки политики повторов, подсказок роли, сводки и классификации ошибок транспорта идут без БД.
