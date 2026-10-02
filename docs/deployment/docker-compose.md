# Docker Compose

> Используйте Compose **v2** (плагин `docker compose`, через пробел).
> `docker-compose` v1 (с дефисом) даёт контейнерам и образам другие имена
> (`project_service_1` вместо `project-service-1`), не видит существующий
> v2-стек и поднимает параллельный набор контейнеров от устаревших образов.

## Сборка и запуск

```powershell
Copy-Item .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

`--build` важен после изменений в `frontend`, `engineering-frontend`, `fsa/frontend`,
`backend` или `fsa/backend`: без него Compose может запустить ранее собранный image. Docker сам переиспользует
безопасные слои (`npm ci`/установка Python-зависимостей), а изменившиеся исходники пересоберёт.
Для полностью чистой проверки используйте `docker compose build --no-cache`.

`price-api` и `fsa-api` сами выполняют `alembic upgrade head` при старте. Данные сохраняются
в volumes `price-db`, `fsa-db` и `minio-data`.

Основные адреса:

- пользовательский UI: `http://localhost:8880/`;
- инженерный UI: `http://localhost:8880/engineering/`;
- основной API: `http://localhost:8801/api/docs`;
- FSA UI: `http://localhost:8881/`;
- FSA API: `http://localhost:8803/docs`.

Для пересборки UI:

```powershell
docker compose up -d --build frontend engineering-frontend fsa-frontend
```

Не используйте `docker compose down -v`, если нужно сохранить базы и файлы.
