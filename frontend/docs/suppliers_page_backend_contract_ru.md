# Страница «Поставщики»: реализованная интеграция и ограничения

Дата актуализации: 16 августа 2026 года.

## 1. Назначение текущей страницы

В live-режиме страница «Поставщики» является интерфейсом загрузки и контроля
разбора прайс-листов. Она работает с заданиями `PriceImport`, а не с полным
доменом карточки поставщика.

Frontend не обращается к базе данных или файловому хранилищу напрямую. Все
операции выполняются через backend API `/api/v1`:

- файл передаётся backend;
- backend сохраняет исходник и создаёт версию прайса;
- Celery worker выполняет разбор;
- frontend опрашивает статус;
- позиции и замечания читаются отдельными paginated-запросами.

Название страницы не означает, что уже реализованы:

- CRUD поставщиков;
- карточка поставщика;
- список его товаров и документов;
- скидки;
- сертификаты и паспорта;
- управление активными версиями прайса.

## 2. Общие интеграционные зависимости

До открытия live-страницы уже должны работать:

1. `POST /api/v1/auth/login` — получение access JWT.
2. `POST /api/v1/auth/refresh` — обновление сессии через HttpOnly cookie.
3. `GET /api/v1/projects` — получение project UUID.
4. Передача `Authorization: Bearer <JWT>`.
5. Передача `X-Project-ID: <project UUID>`.

При смене проекта frontend очищает React Query cache и загружает историю
импортов выбранного проекта заново.

Browser использует JWT. Service key `plk_...` во frontend не хранится и не
передаётся.

## 3. Реализованные backend-операции

### 3.1. Профили моделей разбора

`GET /api/v1/reference/llm-profiles`

Frontend использует только профили с `enabled=true` и показывает:

- название провайдера;
- доступные модели;
- модель по умолчанию.

API не возвращает секреты и внешние base URL.

Провайдер считается доступным только при полной серверной конфигурации:

- OpenAI-compatible: задан `OPENAI_API_KEY`;
- DeepSeek: задан `DEEPSEEK_API_KEY`;
- Yandex Cloud: заданы `YANDEXGPT_API_KEY` и `YANDEXGPT_FOLDER_ID`.

Frontend требует выбрать доступный provider/model перед отправкой прайса. Если
профилей нет, отправка блокируется; пустые значения не подменяются backend
default-профилем.

Backend и Celery worker читают переменные только при создании контейнера. После
изменения LLM-конфигурации их нужно пересоздать командой
`./run.sh reload-llm`. Frontend повторно запрашивает профили при открытии
страницы и возврате фокуса в окно.

### 3.2. История импортов

`GET /api/v1/price-imports?limit=50&offset=0`

Backend поддерживает:

- `status`;
- `limit` от 1 до 200;
- `offset`;
- project scope через `X-Project-ID`.

Текущий frontend запрашивает только первые 50 заданий и не передаёт фильтр
`status`.

Ответ:

```json
{
  "total": 1,
  "items": [
    {
      "id": "uuid",
      "status": "completed",
      "stage": "completed",
      "progress_percent": 100,
      "supplier_id": "uuid",
      "supplier_external_id": "supplier-42",
      "supplier_name": "Поставщик 42",
      "price_version_id": "uuid",
      "filename": "price.xlsx",
      "item_count": 120,
      "warnings_count": 2,
      "errors_count": 0,
      "created_at": "2026-08-16T08:00:00Z"
    }
  ]
}
```

Страница показывает:

- поставщика и его external ID;
- имя файла;
- статус;
- прогресс;
- число позиций;
- дату создания.

Незавершённые задания в истории автоматически обновляются каждые 2 секунды.
Также доступно ручное обновление списка.

### 3.3. Загрузка прайса

`POST /api/v1/price-imports`

Операция доступна:

- browser-пользователю с ролью `admin`;
- внешнему server-to-server клиенту с service key.

Роль backend `user` получает `403`. Во frontend кнопка загрузки скрыта для
пользователя, сопоставленного роли manager, но окончательная проверка всегда
выполняется backend.

Headers:

```http
Authorization: Bearer <access JWT>
X-Project-ID: <project UUID>
Idempotency-Key: ui-<UUID>
```

Multipart-поля, которые отправляет frontend:

- `file` — настоящий бинарный `File`;
- `supplier_external_id` — обязательный код поставщика;
- `supplier_name` — optional display name;
- `currency_hint` — optional ISO currency;
- `provider` — optional enabled provider;
- `model` — optional разрешённая модель.

Backend дополнительно поддерживает, но текущая форма не отправляет:

- `locale`, используется backend default `ru-RU`;
- `callback_url`;
- `policy`, используется backend default `strict`.

Поддерживаемые расширения:

- `.xlsx`;
- `.xlsm`;
- `.xls`;
- `.csv`;
- `.pdf`;
- `.docx`;
- `.doc`;
- `.txt`.

Backend повторно проверяет расширение, непустой файл, валюту, provider/model и
формат `Idempotency-Key`.

Ответ `202`:

```json
{
  "import_id": "uuid",
  "status": "queued",
  "status_url": "/api/v1/price-imports/<uuid>",
  "result_url": "/api/v1/price-imports/<uuid>/result",
  "replayed": false
}
```

Frontend выбирает созданный `import_id` и начинает polling. Если backend
возвращает `replayed=true`, UI сообщает, что открыт ранее созданный импорт.

`Idempotency-Key` создаётся при открытии/сбросе формы и обновляется при выборе
другого файла. Повтор одинакового запроса с тем же ключом не создаёт второе
задание. Другой payload с тем же ключом возвращает `409`.

### 3.4. Статус импорта

`GET /api/v1/price-imports/{import_id}`

Frontend опрашивает endpoint каждые 2 секунды для промежуточных статусов:

- `queued`;
- `parsing`;
- `generating_rule`;
- `compiling_rule`;
- `reparsing`;
- `quality_check`.

Terminal statuses:

- `completed`;
- `review_required`;
- `needs_ocr`;
- `failed`.

Страница показывает:

- текущий этап;
- процент выполнения;
- число позиций;
- число предупреждений и ошибок;
- `error_message`, если оно присутствует.

После terminal status polling останавливается и становятся доступны вкладки
позиций и замечаний.

Backend также возвращает `completeness`, `telemetry` и `webhook`, но текущая
страница их не отображает.

### 3.5. Разобранные позиции

`GET /api/v1/price-imports/{import_id}/items?limit=50&offset=<offset>`

До terminal status backend возвращает `409 IMPORT_NOT_TERMINAL`. Frontend не
вызывает endpoint до завершения polling.

Ответ имеет вид:

```json
{
  "import_id": "uuid",
  "status": "completed",
  "total": 120,
  "items": []
}
```

Через типизированный mapper страница показывает:

- исходное наименование;
- артикул;
- общую категорию или исходный category path;
- единицу измерения;
- первую найденную цену;
- до четырёх извлечённых характеристик;
- лист и строку исходного файла.

Mapper сохраняет семантику цены:

- точное значение;
- диапазон;
- цена «от»;
- «по запросу»;
- нераспознанное значение.

Пагинация выполняется backend, размер страницы — 50.

### 3.6. Ошибки и предупреждения разбора

`GET /api/v1/price-imports/{import_id}/issues?limit=50&offset=<offset>`

Backend также поддерживает `severity=error|warning`, но текущий frontend не
показывает отдельный фильтр severity.

Страница отображает:

- severity;
- машинный код;
- лист, строку и колонку;
- исходное значение;
- сообщение.

Пагинация выполняется backend, размер страницы — 50.

### 3.7. Полный результат

`GET /api/v1/price-imports/{import_id}/result`

Endpoint существует и возвращает status, source, provenance, все items и все
issues одним ответом. Текущая страница его не использует, потому что отдельные
paginated endpoints безопаснее для больших прайсов.

## 4. Реализованная frontend-архитектура

Основные файлы:

- `frontend/src/pages/suppliers/index.tsx` — live и MSW-ветки страницы;
- `frontend/src/shared/api/backend-types.ts` — DTO импорта;
- `frontend/src/shared/api/resources.ts` — HTTP-функции;
- `frontend/src/shared/api/resource-hooks.ts` — React Query hooks и polling;
- `frontend/src/shared/api/price-import-form.ts` — построение `FormData`;
- `frontend/src/shared/api/llm-profiles.ts` — выбор разрешённого профиля;
- `frontend/src/shared/api/mappers.ts` — item/issue/status view models;
- `frontend/src/shared/api/client.ts` — JWT, refresh, `X-Project-ID`, ошибки.

MSW-режим сохранён отдельно. Live-компонент не использует mock ID, mock
поставщиков или строковые имена файлов вместо настоящего `File`.

Ошибки FastAPI `detail`, validation arrays и `message` преобразуются в единое
пользовательское сообщение. Отдельно обрабатываются `401`, `403`, `409`,
`422` и transport errors.

## 5. Что страница пока не реализует

### 5.1. Полный справочник поставщиков

`GET /api/v1/suppliers` и `POST /api/v1/suppliers` существуют, но live-страница
«Поставщики» их не использует. Эти операции имеют другой контракт:
`code/name`, а импорт использует `supplier_external_id/supplier_name` внутри
client scope.

До подключения CRUD необходимо определить единый источник identity:

- является ли `Supplier.code` публичным идентификатором;
- как он связан с `supplier_external_id`;
- должен ли пользователь выбирать существующего поставщика или вводить новый
  external ID;
- как предотвращаются дубли из-за опечаток;
- должен ли список поставщиков быть project-scoped.

Без этого нельзя механически заменить поле кода на выпадающий список.

### 5.2. Пагинация и фильтры истории

Backend уже поддерживает `status`, `limit`, `offset`, но UI показывает только
первые 50 заданий.

Frontend-доработка:

- добавить пагинацию истории;
- добавить фильтр статуса;
- сохранять выбранный `import_id`, если он остаётся в текущей выборке;
- корректно переходить на предыдущую страницу после уменьшения `total`.

Backend-доработка для этого не требуется.

### 5.3. Review workflow

Статус `review_required` отображается как terminal, но страница не подключает:

- `GET /api/v1/review/{import_id}`;
- review sections;
- редактирование mapping/rule;
- validate/apply/activate-rule.

Нужен отдельный frontend vertical slice с admin RBAC, revision conflict
handling и API-тестами. Нельзя считать просмотр items/issues завершением
ручной проверки.

### 5.4. OCR

Для `needs_ocr` показывается статус, но нет запуска OCR, повторной обработки
или связи с отдельным OCR-сервисом.

Нужен согласованный server-to-server OCR pipeline. OCR token нельзя помещать
во frontend.

### 5.5. Retry, cancel и delete

В текущем публичном контракте нет browser-операций:

- отменить выполняющийся импорт;
- повторить failed import как новое задание;
- удалить импорт и исходный файл.

Такие действия требуют backend endpoint, правил идемпотентности, аудита и
определения влияния на `PriceVersion` и уже разобранные позиции.

### 5.6. Категории и версии

Страница отображает категорию позиции, но не выполняет:

- ручное назначение общей категории;
- mapping supplier categories;
- auto-classify;
- активацию версии прайса;
- просмотр оригинала, preview или export.

Эти возможности принадлежат ресурсам `/catalog`, `/price-items` и
`/price-versions`; подключать их следует отдельными типизированными hooks, а
не расширением `/price-imports`.

### 5.7. Карточка поставщика и документы

Не подключены:

- контактные данные;
- товары поставщика как отдельная карточка;
- скидки;
- сертификаты и паспорта;
- другие документы;
- сроки действия документов;
- FSA/B24 evidence.

Для документов отсутствует согласованный публичный CRUD/upload API.

### 5.8. Остальные доступные поля импорта

Frontend пока не показывает:

- `completeness`;
- детальную `telemetry`;
- webhook delivery;
- provenance целиком;
- выбранное parse rule и revision;
- provider/model/prompt version результата.

Часть данных уже возвращается backend и может быть подключена frontend-only
изменением. Для больших JSON-полей нужен отдельный компактный UI, а не
добавление сырых данных в основную таблицу.

## 6. Рекомендуемая дальнейшая декомпозиция

### Frontend-only

1. Пагинация и фильтр статуса истории импортов.
2. Фильтр severity для issues.
3. Компактное отображение completeness и parser provenance.
4. Ссылка на отдельный экран review для `review_required`.
5. Явные действия для `failed` и `needs_ocr`, когда появится backend-контракт.

### Backend и продуктовое решение

1. Единая identity поставщика для `/suppliers` и `/price-imports`.
2. Project scope справочника поставщиков.
3. Retry/cancel/delete lifecycle.
4. OCR orchestration.
5. Document/certificate API.
6. Права manager/admin на upload, review и category mapping.

### Отдельные существующие resource API

1. `/review` — ручная проверка.
2. `/catalog` — общий классификатор и supplier mappings.
3. `/price-items` — позиции поверх всех версий.
4. `/price-versions` — оригинал, preview, export, mapping и status.

Не следует добавлять эти операции под `/price-imports` только ради одной
frontend-страницы.

## 7. Проверки текущей интеграции

Исторический live smoke от 8 августа 2026 года зафиксирован в разделе 13
`coordination.md`:

- admin login;
- выбор проекта;
- multipart upload `202`;
- idempotent replay;
- polling `queued` → `completed`;
- чтение items и issues;
- `403` для user upload;
- `422` для некорректного idempotency key;
- refresh и logout;
- Vite proxy flow.

Unit-тесты покрывают:

- состав `FormData`, включая provider/model;
- выбор только enabled LLM profiles;
- mapper статусов;
- цены, категории, характеристики и source coordinates;
- mapper issues.

Backend tests дополнительно проверяют import service, status transitions,
idempotency, project/client scope, terminal result access и ошибки разбора.
