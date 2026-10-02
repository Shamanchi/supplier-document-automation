"""RBAC-конфигурация: какие права закреплены за ролью.

Права — это набор действий, который экран профиля показывает пользователю и
редактирует под ролью `sysadmin` (docs/api/profile/README.md §Управление правами).
Хранятся в таблице `role_permissions`; каталог допустимых значений и обязательный
минимум каждой роли заданы здесь, потому что это модель прав приложения, а не
пользовательские данные.

**Что это НЕ делает.** Проверки доступа в коде сегодня опираются на роль
(`WRITE_ROLES` в checks, `require_roles` в identity), а не на эти строки: значения
каталога — русские подписи для UI, а не машинные коды, и гейтить ими эндпоинты
нельзя. Контракт обещает «RBAC применяется к последующим проверкам доступа»;
чтобы это стало правдой, каталогу нужны коды прав и перевод проверок на них —
отдельная задача, см. отчёт.

Перенесено из supplier-document-automation без изменений (docs/migration/02-backend-identity.md):
каталоги прав и роли в обеих фазах совпадают, слияние не потребовалось.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorCode
from app.modules.identity.models import ROLES, RolePermission

#: Все права, которые может выдать редактор. Порядок — как на экране профиля.
PERMISSION_CATALOG: tuple[str, ...] = (
    "Создание заявок",
    "Загрузка файлов",
    "Ручной ввод параметров",
    "Предложение синонимов",
    "Управление поставщиками",
    "Управление товарами",
    "Подтверждение синонимов",
    "Управление справочниками",
    "Управление пользователями",
    "Назначение ролей",
    "Просмотр логов",
    "Просмотр заявок",
    "Просмотр отчётов",
)

#: Базовый набор роли. Он же — обязательный минимум: снять такое право нельзя,
#: иначе роль теряет смысл (profile/README.md: «обязательные права сервер не даёт
#: снять»). Дополнительные права из каталога роль может получать и терять.
DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "manager": ("Создание заявок", "Загрузка файлов", "Ручной ввод параметров"),
    "admin": (
        "Управление поставщиками",
        "Подтверждение синонимов",
        "Управление справочниками",
    ),
    "sysadmin": ("Управление пользователями", "Назначение ролей", "Просмотр логов"),
    "viewer": ("Просмотр заявок", "Просмотр отчётов"),
}


class PermissionErrorCode:
    """Коды ошибок модуля (docs/api/profile/README.md §Ошибки)."""

    INVALID_ROLE = "INVALID_ROLE"
    MANDATORY_PERMISSION_REMOVED = "MANDATORY_PERMISSION_REMOVED"
    PERMISSION_ADMINISTRATION_DENIED = "PERMISSION_ADMINISTRATION_DENIED"


def mandatory_for(role: str) -> tuple[str, ...]:
    """Права роли, которые нельзя снять."""
    return DEFAULT_ROLE_PERMISSIONS.get(role, ())


class PermissionsService:
    """Чтение и замена наборов прав. Владелец таблицы `role_permissions`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def full_map(self) -> dict[str, list[str]]:
        """Права всех ролей в порядке каталога.

        Порядок из каталога, а не из БД: экран показывает права списком, и
        произвольный порядок строк выглядел бы как случайная перестановка.
        """
        rows = await self._session.execute(
            select(RolePermission.role, RolePermission.permission)
        )
        stored: dict[str, set[str]] = {role: set() for role in ROLES}
        for role, permission in rows.all():
            stored.setdefault(role, set()).add(permission)
        return {
            role: [name for name in PERMISSION_CATALOG if name in permissions]
            for role, permissions in stored.items()
        }

    async def map_for(
        self, roles: list[str], *, full_map: bool
    ) -> dict[str, list[str]]:
        """Карта прав для ответа профиля.

        Полную карту получает только sysadmin — он редактирует RBAC. Остальным
        отдаётся лишь их собственный набор: урезание выполняет сервер, а не UI
        (profile/README.md §GET /profile).
        """
        everything = await self.full_map()
        if full_map:
            return everything
        return {role: everything.get(role, []) for role in roles}

    async def replace(self, role: str, permissions: list[str]) -> list[str]:
        """Заменяет набор прав роли целиком (PUT-семантика на набор).

        Возвращает сохранённый набор в порядке каталога.
        """
        if role not in ROLES:
            raise ApiError(
                PermissionErrorCode.INVALID_ROLE,
                f"Неизвестная роль: {role}",
                status_code=422,
                details={"allowed": list(ROLES)},
            )

        requested = set(permissions)
        unknown = sorted(requested - set(PERMISSION_CATALOG))
        if unknown:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "В наборе есть права, которых нет в каталоге",
                status_code=422,
                details={"unknown": unknown},
            )

        missing = [name for name in mandatory_for(role) if name not in requested]
        if missing:
            raise ApiError(
                PermissionErrorCode.MANDATORY_PERMISSION_REMOVED,
                f"Обязательные права роли {role} снять нельзя",
                status_code=422,
                details={"mandatory": missing},
            )

        await self._session.execute(
            delete(RolePermission).where(RolePermission.role == role)
        )
        for name in PERMISSION_CATALOG:
            if name in requested:
                self._session.add(RolePermission(role=role, permission=name))
        await self._session.flush()
        return [name for name in PERMISSION_CATALOG if name in requested]

    async def ensure_defaults(self) -> int:
        """Заполняет таблицу базовыми наборами, если она пуста.

        Только при полной пустоте: иначе повторный старт возвращал бы права,
        которые sysadmin сознательно снял.
        """
        existing = await self._session.scalar(
            select(func.count()).select_from(RolePermission)
        )
        if existing:
            return 0

        created = 0
        for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
            for permission in permissions:
                self._session.add(RolePermission(role=role, permission=permission))
                created += 1
        await self._session.flush()
        return created
