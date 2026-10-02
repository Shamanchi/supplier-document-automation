"""Модуль identity (перенос из supplier-document-automation, docs/migration/02-backend-identity.md).

Канон схемы и логики — фаза 3: единые таблицы `users`, `user_roles`,
`role_permissions`, `refresh_sessions` плюс `audit_events`, `external_links`,
`tender_refs`. Старый `app/identity/` фазы 2 удаляется, его потребители
переведены на этот путь.

Импорт пакета регистрирует ORM-модели в общем метаданных — на этом держится
`alembic/env.py`.
"""
from __future__ import annotations

from app.modules.identity import models  # noqa: F401
