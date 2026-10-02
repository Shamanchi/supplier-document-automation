"""Очередь загрузок реестра: вид файла, статус, ссылка на объект в S3.

Файлы реестра кладут руками, поэтому приёму нужен собственный журнал:
без него не видно, что именно загрузили, чем закончилось и когда.

Revision ID: 0003_fsa_imports
Revises: 0002_fsa_text
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_fsa_imports"
down_revision = "0002_fsa_text"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("kind", sa.String(length=16), server_default="index"),
    sa.Column("status", sa.String(length=16), nullable=False, server_default="completed"),
    sa.Column("filename", sa.Text(), server_default=""),
    sa.Column("storage_key", sa.Text(), server_default=""),
    sa.Column("size_bytes", sa.Integer(), server_default="0"),
    sa.Column("sha256", sa.String(length=64), server_default=""),
    sa.Column("task_id", sa.String(length=64), server_default=""),
    sa.Column("error", sa.Text(), server_default=""),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("registry_import", column)
    op.create_index("ix_registry_import_status", "registry_import", ["status"])


def downgrade() -> None:
    op.drop_index("ix_registry_import_status", table_name="registry_import")
    for column in _COLUMNS:
        op.drop_column("registry_import", column.name)
