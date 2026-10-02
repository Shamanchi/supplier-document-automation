# Первичная настройка

1. Создайте `.env` из `.env.example`.
2. Задайте стойкие `APP_AUTH_SECRET` и `PHASE3_AUTH_SECRET`.
3. Замените bootstrap-пароли `APP_ADMIN_PASSWORD`, `APP_USER_PASSWORD`,
   `APP_SYSADMIN_PASSWORD` и `APP_VIEWER_PASSWORD`.
4. Выполните `docker-compose up -d --build`. Оба API автоматически применят Alembic-миграции.
5. Проверьте `http://localhost:8880/api/v1/health` и `http://localhost:8803/health`.
6. Войдите под bootstrap-учётной записью.

При первом старте и только при пустой таблице пользователей backend создаёт
учётные записи из `.env`. Пароли сохраняются в `price-db` как Argon2-хеши.
Последующее изменение пароля в `.env` не меняет существующую запись в БД.

Для deterministic-разбора можно импортировать встроенные правила:

```powershell
docker-compose run --rm price-api python -m app.seed import /srv/seeds/parse_rules_builtin.json
```
