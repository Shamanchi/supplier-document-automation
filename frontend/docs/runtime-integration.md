# Интеграция frontend с runtime

Основной frontend — единое React/FSD-приложение из `frontend/`. Production-сборка работает без MSW и обращается к backend через nginx по префиксу `/api/v1`.

## Подключено к live backend

- login, refresh, logout и текущий пользователь;
- общий Phase 3 JWT-контракт;
- проекты и `X-Project-ID`;
- поставщики и импорт прайс-листов;
- позиции прайсов и страница товаров;
- parse rules, review и model-assisted rule proposals при наличии внешнего ключа.

FSA UI использует тот же login/refresh flow через `price-api`; отдельная форма `FSA_API_TOKEN` больше не является пользовательским сценарием.

## Сохранено в UI, но требует Phase 3 backend

- заявки `/orders` и их полный жизненный цикл;
- dashboard по заявкам;
- документы, сертификаты и результаты технических проверок;
- профиль и полный permission catalog Phase 3.

Эти страницы не должны подменяться демонстрационными данными в live-режиме. После размещения Phase 2 и Phase 3 за общим gateway их маршруты подключаются к соответствующим backend-сервисам, а JWT остаётся единым.

## Сопутствующие документы

- `frontend/API.md` — описание frontend API-контрактов;
- `frontend/api-contracts.json` — OpenAPI snapshot интеграционного UI;
- корневой `README.md` — запуск всего локального runtime.
