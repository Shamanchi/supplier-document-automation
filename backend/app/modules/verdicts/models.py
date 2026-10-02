"""ORM-модель модуля `verdicts`.

Владение таблицей `item_verdicts` — по docs/architecture/BACKEND_MODULE_MAP.md.
Модель жила в `checks/models.py`, пока модуля не было; здесь она на своём месте,
а `checks` и `dashboard` читают её отсюда.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class ItemVerdict(Base):
    """Вердикт позиции: computed против решения менеджера.

    Два поля вместо одного принципиальны: `computed_verdict` — результат правил,
    и он остаётся видимым, даже когда менеджер решил иначе. При расхождении
    комментарий обязателен (verdicts §Подтверждение).
    """

    __tablename__ = "item_verdicts"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    computed_verdict: Mapped[str] = mapped_column(
        pg_enum("verdict_value"), nullable=False
    )
    manager_verdict: Mapped[str] = mapped_column(
        pg_enum("verdict_value"), nullable=False
    )
    #: Распределение статусов сравнений, на котором построен вердикт.
    basis: Mapped[dict | None] = mapped_column(JSONB)
    comment: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
