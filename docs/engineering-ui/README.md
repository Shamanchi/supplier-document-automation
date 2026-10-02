# Инженерный UI

Инженерный интерфейс доступен по `/engineering/` и предназначен для настройки
разбора прайс-листов. Это отдельная frontend-сборка, но источник данных и правил
тот же, что у пользовательского UI: основной `backend` и PostgreSQL `price-db`.

Доступ разрешён только роли `sysadmin`. Авторизация выполняется через основной
`/api/v1/auth/login`, `/api/v1/auth/refresh` и `/api/v1/auth/me`; отдельного
инженерного токена или второй базы пользователей нет.

В основном UI кнопка «Инженерный UI» находится в верхней панели рядом с email и
кнопкой выхода. Видимость определяется по наличию `sysadmin` в массиве `roles` ответа
`/api/v1/auth/me`, а не только по агрегированному полю `role`.

## Страницы и API

| Страница | Назначение | Backend API |
| --- | --- | --- |
| `/` и `/imports/:id` | импорты и состояние обработки | `/price-imports` |
| `/review-queue` | очередь проблемных секций | `/review/queue`, `/review/{import_id}` |
| `/data` | позиции и версии прайсов | `/price-items`, `/price-versions` |
| `/rules` и `/rules/:ruleId` | правила, mapping и lifecycle | `/parse-rules` |
| `/categories` | категории поставщиков | `/catalog/*` |
| `/playground` | проверка подбора товаров | `/matching/*` |
| `/api-access` | service clients и ключи | `/service-clients` |

## Сценарий настройки

Пользовательский UI показывает переход для `sysadmin`, если импорт получил
`review_required`, `needs_mapping` или `mapping_required`:

```text
/engineering/review?import=<import_id>
```

Далее инженер выполняет `mapping → validate (dry-run) → apply → activate-rule`.
Активное правило сохраняется в основной таблице `parse_rules` и используется
последующими импортами подходящего формата.

## Runtime

В Docker Compose сервис называется `engineering-frontend`. Основной nginx
маршрутизирует `/engineering/` в этот контейнер, а `/api/v1/` направляет в
`price-api`. Отдельный внешний порт инженерному UI не нужен.
