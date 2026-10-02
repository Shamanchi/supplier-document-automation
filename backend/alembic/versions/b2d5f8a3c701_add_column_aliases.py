"""Add the column-caption role dictionary.

Роль колонки сейчас выводится только встроенным словарём в
``app.parsing.headers``.  Подпись, разобранную один раз человеком или
подтверждённую моделью, некуда записать, поэтому следующий прайс с той же
подписью снова уходит в очередь.

Таблица хранит подпись в нижнем регистре со схлопнутыми пробелами и область
действия: глобальная запись работает для всех прайсов, supplier-запись
перекрывает её для одного поставщика, price_file-запись — для одного файла.
Исключения заложены сразу: у одного поставщика «расч.» — оптовая цена, у
другого — расчётная величина.

Revision ID: b2d5f8a3c701
Revises: a1b2c3d4e5f6
"""

from alembic import op
import sqlalchemy as sa


revision = "b2d5f8a3c701"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "column_aliases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("price_kind", sa.String(length=16), nullable=True),
        sa.Column("vat_mode", sa.String(length=16), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column("supplier_id", sa.Uuid(), nullable=True),
        sa.Column("price_file_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="human"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["price_file_id"], ["price_files.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "alias", "scope", "supplier_id", "price_file_id", name="uq_column_alias_scope"
        ),
    )
    op.create_index("ix_column_aliases_alias", "column_aliases", ["alias"])


def downgrade() -> None:
    op.drop_index("ix_column_aliases_alias", table_name="column_aliases")
    op.drop_table("column_aliases")
