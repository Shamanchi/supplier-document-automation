"""Модуль `profile` (docs/api/profile/README.md).

BFF-агрегат экрана профиля. Аккаунт-часть строится тем же кодом, что и
`GET /auth/me` (`current_user_payload`), чтобы не возникло второго источника
истины о пользователе.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from app.core.errors import ApiError
from app.core.schemas import BaseSchema
from app.modules.identity.dependencies import (
    CurrentUser,
    PermissionsServiceDep,
    SessionDep,
)
from app.modules.identity.models import ROLES, AuditEvent
from app.modules.identity.permissions import (
    PERMISSION_CATALOG,
    PermissionErrorCode,
    mandatory_for,
)
from app.modules.identity.router import initials_of

router = APIRouter(prefix="/profile", tags=["profile"])


class Notification(BaseSchema):
    id: UUID
    check_batch_id: UUID
    title: str
    type: Literal["discount", "vat"]
    status: Literal["pending", "approved", "rejected"]
    created_at: datetime


class ProfileResponse(BaseSchema):
    name: str
    email: str
    initials: str
    role: str
    roles: list[str]
    notifications: list[Notification]
    permissions: dict[str, list[str]]
    #: Все права, которые может выдать редактор. Каталог держит сервер, иначе
    #: список на экране разошёлся бы с тем, что бэкенд согласен принять.
    permission_catalog: list[str]
    #: Права, которые нельзя снять, по роли. Экран блокирует их чекбоксы, но
    #: авторитетная проверка — на сервере (profile/README.md §Управление правами).
    mandatory_permissions: dict[str, list[str]]


class PermissionsUpdate(BaseSchema):
    role: str
    permissions: list[str] = Field(max_length=len(PERMISSION_CATALOG))


class NotificationDecision(BaseSchema):
    status: Literal["approved", "rejected"]


class RoleUpdate(BaseSchema):
    role: str


async def _profile_body(
    user: CurrentUser, permissions: PermissionsServiceDep
) -> ProfileResponse:
    roles = user.role_names
    is_sysadmin = "sysadmin" in roles
    visible_roles = ROLES if is_sysadmin else roles
    return ProfileResponse(
        name=user.display_name,
        email=user.email,
        initials=initials_of(user),
        role=roles[0] if roles else "viewer",
        roles=roles,
        # Уведомления — запросы на подтверждение скидки/НДС в КП. Сущность
        # принадлежит контуру расчёта КП Фазы 2, который ещё не реализован,
        # поэтому список пуст. Появится вместе с модулем calculations.
        notifications=[],
        permissions=await permissions.map_for(roles, full_map=is_sysadmin),
        permission_catalog=list(PERMISSION_CATALOG),
        mandatory_permissions={
            role: list(mandatory_for(role)) for role in visible_roles
        },
    )


@router.get("", response_model=ProfileResponse)
async def get_profile(
    user: CurrentUser, permissions: PermissionsServiceDep
) -> ProfileResponse:
    """Агрегат экрана: аккаунт, уведомления, права по ролям."""
    return await _profile_body(user, permissions)


@router.patch("/permissions", response_model=ProfileResponse)
async def update_permissions(
    payload: PermissionsUpdate,
    user: CurrentUser,
    permissions: PermissionsServiceDep,
    session: SessionDep,
) -> ProfileResponse:
    """Заменяет набор прав роли целиком. Только для `sysadmin`.

    Это конфигурация модели прав (роль → права), а не свойство профиля: связка
    «пользователь → роль» остаётся за `admin/users`.
    """
    if "sysadmin" not in user.role_names:
        raise ApiError(
            PermissionErrorCode.PERMISSION_ADMINISTRATION_DENIED,
            "Управление правами доступно только роли sysadmin",
            status_code=403,
        )

    saved = await permissions.replace(payload.role, payload.permissions)
    # Правка RBAC — административное действие: без записи в журнал нельзя
    # ответить на вопрос, кто и когда снял право.
    session.add(
        AuditEvent(
            actor_id=user.id,
            action="rbac.permissions_replaced",
            entity_type="role",
            reason=f"{payload.role}: {', '.join(saved) or 'пустой набор'}",
        )
    )
    return await _profile_body(user, permissions)


@router.patch("/notifications/{notification_id}", response_model=ProfileResponse)
async def decide_notification(
    notification_id: UUID,
    payload: NotificationDecision,
    user: CurrentUser,
    permissions: PermissionsServiceDep,
) -> ProfileResponse:
    """Подтверждение или отклонение уведомления.

    Не реализовано осознанно. Уведомление — запрос на подтверждение скидки или
    ставки НДС в КП: решение обязано выполнить доменное действие над расчётом
    (profile/README.md §Уведомления). Ни таблицы уведомлений, ни модуля
    `calculations` в контуре ещё нет, а сохранять решение, которое ничего не
    меняет в КП, хуже явного отказа: экран показал бы «подтверждено» там, где
    ничего не произошло.
    """
    raise ApiError(
        "KP_NOTIFICATIONS_NOT_IMPLEMENTED",
        "Уведомления КП появятся вместе с модулем calculations Фазы 2",
        status_code=501,
        details={"notificationId": str(notification_id), "status": payload.status},
    )


@router.patch("/role", response_model=ProfileResponse)
async def change_role(payload: RoleUpdate, user: CurrentUser) -> ProfileResponse:
    """Смена собственной роли — запрещена.

    В контракте это демонстрационный путь для показа RBAC без бэкенда (MSW).
    С реальным бэкендом самоповышение прав недопустимо: назначение ролей — это
    операция `sysadmin` через `PUT /admin/users/{user_id}/roles`.
    """
    raise ApiError(
        "SELF_ROLE_CHANGE_FORBIDDEN",
        "Свою роль менять нельзя: назначение ролей выполняет sysadmin"
        " через /admin/users/{user_id}/roles",
        status_code=403,
        details={"requestedRole": payload.role},
    )
