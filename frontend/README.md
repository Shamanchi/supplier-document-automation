# DocPrice Frontend

Основной React-интерфейс для заявок, поставщиков, прайс-листов, товаров, сертификатов и справочников.

## Локальная разработка

Требуется Node.js 20 или новее.

```powershell
npm ci
Copy-Item .env.example .env
npm run dev
```

Dev server: `http://localhost:5173`.

```dotenv
VITE_API_BASE_URL=/api/v1
VITE_API_PROXY_TARGET=http://127.0.0.1:8801
VITE_ENABLE_MSW=false
```

Для автономного mock-режима задайте `VITE_ENABLE_MSW=true`.

## Команды

```powershell
npm run typecheck
npm run test:unit
npm run lint
npm run build
npm run test:e2e
```

Production-сборка в составе проекта:

```powershell
docker-compose up -d --build frontend engineering-frontend
```

## Авторизация и роли

В production роль не переключается через `localStorage`. Пользователь и роли приходят из `GET /api/v1/auth/me` через DB-backed сессию.

`manager` имеет ограниченный доступ, `admin` управляет рабочими сущностями, `sysadmin` дополнительно управляет правами и получает переход в инженерный UI, `viewer` работает в режиме чтения.

Backend возвращает агрегированное поле `role` и полный массив `roles`. Для проверки `sysadmin` frontend использует массив `roles`, поскольку поле `role` у такого пользователя может иметь значение `admin`.

Кнопка «Инженерный UI» находится в верхней панели рядом с email и кнопкой выхода и ведёт на `/engineering/`. На странице поставщиков также появляется контекстная кнопка настройки правила для импортов, требующих mapping.

## Маршруты

- `/dashboard` — сводка
- `/orders` — заявки
- `/suppliers` — поставщики и загрузка прайсов
- `/goods` — товары
- `/certificates` — документы
- `/dictionary` — справочники
- `/history` — история проверок
- `/management` — системные настройки
- `/profile` — профиль, роли и права
- `/engineering/` — отдельная сборка инженерного UI

Основной nginx проксирует `/api/*` в `price-api`, а `/engineering/*` — в `engineering-frontend`.

Дополнительно: [инженерный UI](../docs/engineering-ui/README.md), [аутентификация](../docs/security/authentication.md).
