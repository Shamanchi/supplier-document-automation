"""Расширить текстовые колонки реестра.

В наборе открытых данных поля вроде «Номер бланка приложений» содержат
списки номеров через точку с запятой, а ИНН — перечисление нескольких
изготовителей. Ограничение длины отсекало реальные записи на импорте.

Revision ID: 0002_fsa_text
Revises: 0001_fsa
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_fsa_text"
down_revision = "0001_fsa"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("status_raw", sa.String(length=64)),
    ("object_type", sa.String(length=128)),
    ("blank_number", sa.String(length=64)),
    ("annex_blank_number", sa.String(length=64)),
    ("applicant_inn", sa.String(length=32)),
    ("manufacturer_inn", sa.String(length=32)),
    ("manufacturer_ogrn", sa.String(length=32)),
    ("source_slice", sa.String(length=128)),
)


def upgrade() -> None:
    for name, _ in _COLUMNS:
        op.alter_column("registry_certificate", name, type_=sa.Text())


def downgrade() -> None:
    for name, previous in _COLUMNS:
        op.alter_column("registry_certificate", name, type_=previous)
