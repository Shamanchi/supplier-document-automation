"""ORM-модели модуля identity.

Владение таблицами — по docs/architecture/BACKEND_MODULE_MAP.md (фаза 3):
`users`, `user_roles`, `refresh_sessions` принадлежат этому модулю, другие
модули к ним напрямую не обращаются.

Схема создаётся миграцией из schema.sql; модели описывают только те колонки,
которые нужны коду. `Base` общий для всех модулей, чтобы SQLAlchemy видел связи
(шаг миграции 02: база объявлена в `app.db`, реэкспортируется `app.models`).

Перенесено из supplier-document-automation без изменений (docs/migration/02-backend-identity.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import pg_enum
from app.db import Base

#: Роли из enum user_role (schema.sql). viewer добавлен по факту реализации,
#: его права — открытый вопрос DB-07.
ROLES = ("manager", "admin", "sysadmin", "viewer")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    external_subject: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(pg_enum("user_status"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def role_names(self) -> list[str]:
        return [r.role for r in self.roles]

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"


class UserRole(Base):
    __tablename__ = "user_roles"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(pg_enum("user_role"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="roles")


class RolePermission(Base):
    """Право, закреплённое за ролью (RBAC-конфигурация, миграция 0003 фазы 3).

    Одна строка на пару «роль → право»: составной первичный ключ сам запрещает
    дубликаты. Каталог допустимых значений — в `identity/permissions.py`.
    """

    __tablename__ = "role_permissions"

    role: Mapped[str] = mapped_column(pg_enum("user_role"), primary_key=True)
    permission: Mapped[str] = mapped_column(Text, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RefreshSession(Base):
    """Refresh-сессия с ротацией.

    Хранится только хеш токена: утечка таблицы не даёт войти. `family_id`
    объединяет цепочку ротаций — по нему обнаруживается повторное использование
    уже ротированного токена и отзывается вся цепочка (контракт auth §Security).
    """

    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class AuditEvent(Base):
    """Append-only журнал. Пишется, но не читается модулем identity."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExternalLink(Base):
    """Связь локальной сущности с внешним ID.

    Пишется и читается модулем `integrations/bitrix`: связь заявки со сделкой,
    импортированного документа с файлом портала, опубликованного отчёта с
    файлом и комментарием таймлайна. Здесь модель живёт потому, что таблица
    закреплена за `identity` в BACKEND_MODULE_MAP.md.
    """

    __tablename__ = "external_links"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_entity_type: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: URL, имя папки и прочий контекст связи. Атрибут назван иначе, чем
    #: колонка: `metadata` занято в declarative-базе SQLAlchemy.
    #: `with_variant` — адаптация к фазе 2: тесты прайс-контура поднимают
    #: схему в SQLite, который не умеет JSONB (в PostgreSQL тип не меняется).
    link_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TenderRef(Base):
    """Ссылка на сделку. Dashboard берёт отсюда заказчика и номер тендера."""

    __tablename__ = "tender_refs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization: Mapped[str] = mapped_column(Text, nullable=False)
    tender_id: Mapped[str] = mapped_column(Text, nullable=False)
    bitrix_deal_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
