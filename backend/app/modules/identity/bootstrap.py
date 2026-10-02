"""Создание учётных записей при первом старте.

Срабатывает только если таблица `users` пуста — так повторный запуск не меняет
заведённых пользователей и не восстанавливает удалённых. Пароли берутся из
настроек; значения по умолчанию годятся лишь для разработки, поэтому при них
приложение пишет предупреждение в лог.

Адаптация к фазе 2 (docs/migration/02-backend-identity.md, п. 3): вместо
`<role>@bootstrap_email_domain` заводятся развёртываемые аккаунты фазы 2 из
`run.sh` (`app_admin_username` → admin, `app_user_username` → manager,
`app_sysadmin_username` → sysadmin, `app_viewer_username` → viewer). Схема P3
(`<role>@<domain>`) добавляется дополнительно, только если заданы
`bootstrap_*_password`, — для локальной разработки.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password

logger = logging.getLogger(__name__)

#: Роль → (отображаемое имя, атрибут настроек с логином, атрибут с паролем)
#: фазы 2. Логины задаются env-файлом, пароли генерирует run.sh.
_DEPLOY_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    ("admin", "Администратор", "app_admin_username", "app_admin_password"),
    ("manager", "Менеджер", "app_user_username", "app_user_password"),
    ("sysadmin", "Системный администратор", "app_sysadmin_username", "app_sysadmin_password"),
    ("viewer", "Наблюдатель", "app_viewer_username", "app_viewer_password"),
)

#: Роль → пароль для dev-аккаунтов P3 вида `<role>@<bootstrap_email_domain>`.
_DEV_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("admin", "bootstrap_admin_password"),
    ("manager", "bootstrap_manager_password"),
    ("sysadmin", "bootstrap_sysadmin_password"),
    ("viewer", "bootstrap_viewer_password"),
)


async def ensure_initial_users(session: AsyncSession, settings: Settings) -> int:
    """Заводит стартовые учётки, если пользователей ещё нет.

    Возвращает число созданных записей (0 — если база уже наполнена).
    """
    if not settings.bootstrap_users_enabled:
        return 0

    existing = await session.scalar(select(func.count()).select_from(User))
    if existing:
        return 0

    created = 0
    used_defaults = False
    seen_emails: set[str] = set()

    # Развёртываемые аккаунты фазы 2 — канон для продакшена.
    for role, display_name, username_attr, password_attr in _DEPLOY_ACCOUNTS:
        username = getattr(settings, username_attr)
        password = getattr(settings, password_attr)
        if not password or username in seen_emails:
            continue
        user = User(
            email=username,
            display_name=display_name,
            password_hash=hash_password(password),
            status="ACTIVE",
        )
        user.roles.append(UserRole(role=role))
        session.add(user)
        seen_emails.add(username)
        created += 1

    # Dev-аккаунты P3 — только при явно заданных паролях (локальная разработка).
    for role, password_attr in _DEV_ACCOUNTS:
        password = getattr(settings, password_attr)
        email = f"{role}@{settings.bootstrap_email_domain}"
        if not password or email in seen_emails:
            continue
        user = User(
            email=email,
            display_name=role,
            password_hash=hash_password(password),
            status="ACTIVE",
        )
        user.roles.append(UserRole(role=role))
        session.add(user)
        seen_emails.add(email)
        created += 1
        used_defaults = True

    await session.flush()

    if created:
        if used_defaults:
            logger.warning(
                "Созданы dev-аккаунты %s@%s с bootstrap-паролями. Это допустимо "
                "только для разработки: задайте BOOTSTRAP_*_PASSWORD или уберите "
                "их перед использованием вне локальной среды.",
                "admin",
                settings.bootstrap_email_domain,
            )
        logger.info("Созданы стартовые учётные записи: %d", created)
    return created
