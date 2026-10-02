# Аутентификация

Основной backend использует DB-backed identity в PostgreSQL `price-db`.

API:

- `POST /api/v1/auth/login` — проверка логина и Argon2-хеша пароля;
- `POST /api/v1/auth/refresh` — обновление access JWT по HttpOnly cookie;
- `GET /api/v1/auth/me` — пользователь, роли и permissions;
- `POST /api/v1/auth/logout` — завершение текущей сессии;
- `POST /api/v1/auth/logout-all` — завершение всех сессий пользователя.

Пользовательский и инженерный UI используют один origin, одну refresh-cookie и
один JWT. Инженерный UI дополнительно требует роль `sysadmin`. `.env` используется
только для bootstrap пользователей в пустой базе, а не для проверки каждого входа.

`GET /api/v1/auth/me` возвращает агрегированное поле `role` и полный массив `roles`.
Для пользователя `sysadmin` агрегированное поле может быть равно `admin`, поэтому UI и
проверки доступа к системным функциям должны ориентироваться на наличие `sysadmin` в
массиве `roles`.
