"""ORM models for parameter-level human-in-the-loop review."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ParameterRevision(Base):
    """Append-only manual correction of an extracted raw parameter."""

    __tablename__ = "parameter_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    raw_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_parameters.id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("parameter_revisions.id")
    )
    name_text: Mapped[str | None] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text)
    unit_text: Mapped[str | None] = mapped_column(Text)
    #: Состояние параметра до правки целиком, а не только изменённые поля:
    #: по цепочке ревизий восстанавливается исходное извлечённое значение.
    previous_name_text: Mapped[str | None] = mapped_column(Text)
    previous_value_text: Mapped[str | None] = mapped_column(Text)
    previous_unit_text: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
