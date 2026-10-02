"""ORM-модель модуля `matching`: кандидаты пары requirement↔fact.

Владение таблицей — по docs/architecture/BACKEND_MODULE_MAP.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md): тесты
#: фазы 2 поднимают схему в SQLite, ARRAY(Text) получает JSON-вариант.
from sqlalchemy import JSON  # noqa: E402

ArrayText = ARRAY(Text).with_variant(JSON(), "sqlite")


class MatchCandidate(Base):
    """Кандидат на пару «требование ↔ факт».

    Хранится и отвергнутый кандидат: менеджеру нужно видеть, что система
    рассматривала и почему не выбрала (`rejection_reasons`), иначе ручной выбор
    пары превращается в гадание.
    """

    __tablename__ = "match_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    requirement_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("normalized_parameters.id")
    )
    fact_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("normalized_parameters.id")
    )
    method: Mapped[str] = mapped_column(pg_enum("match_method"), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    rank: Mapped[int | None] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    rejection_reasons: Mapped[list[str]] = mapped_column(
        ArrayText, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
