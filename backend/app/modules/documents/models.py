"""ORM-модели модуля `documents`.

Владение — по docs/architecture/BACKEND_MODULE_MAP.md: `documents` и
`document_versions` принадлежат этому модулю. Модуль `checks` читает их, чтобы
проверить предусловия запуска (есть ли активный REQUIREMENT и FACT) и показать
счётчики документов в карточке позиции.

Роутер модуля (загрузка файлов) появится вместе с `docs/api/uploads` — здесь
пока только модели, нужные соседям.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class Document(Base):
    """Логический документ позиции: роль, профиль и активная версия."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(pg_enum("document_role"), nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id")
    )
    profile: Mapped[dict | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(pg_enum("document_source"), nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentVersion(Base):
    """Неизменяемая версия файла: SHA-256 и ключ в объектном хранилище.

    Таблица защищена триггером `prevent_modify`: UPDATE и DELETE запрещены на
    уровне БД, потому что на версию ссылаются evidence и отчёты.
    """

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # Только metadata: storage_key формируется сервером и не содержит
    # пользовательского имени (uploads §Проверки Backend, пункт 5).
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(pg_enum("document_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
