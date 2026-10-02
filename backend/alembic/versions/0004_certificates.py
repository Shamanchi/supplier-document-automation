"""Реестр сертификатов как источников ТХ.

Revision ID: 0004_certificates
Revises: 0003_role_permissions
Create Date: 2026-08-19

Модуль `certificates` (docs/api/certificates/README.md) — нативный модуль Фазы 3,
которого нет ни в `db/schema.sql`, ни в карте модулей: контракт написан, таблиц
под него не было.

Файл сертификата хранится обычным документом: контракт требует «создаёт
immutable DocumentVersion (role FACT)», и заводить второе хранилище версий ради
той же сущности незачем — тогда переиспользуются и триггер неизменяемости, и
дедупликация по SHA-256, и проверки содержимого из `uploads`.

Ради этого снимается `NOT NULL` с `documents.check_item_id`: колонка появилась
в предположении, что документ всегда принадлежит позиции проверки, а сертификат
регистрируется до и вне проверок. Расхождение с `db/schema.sql` отмечено в
docs/todo/certificates.md.

Статус сертификата (`ACTIVE`/`EXPIRED`) не хранится: он выводится из
`valid_until` и менялся бы сам собой со временем, а хранимое поле пришлось бы
обновлять фоновой задачей и разъезжалось бы с датой.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_certificates"
down_revision = "0003_role_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("documents", "check_item_id", nullable=True)
    op.execute(
        "COMMENT ON COLUMN documents.check_item_id IS"
        " 'Позиция проверки; пусто у документов вне проверки — например,"
        " у файла сертификата.'"
    )

    op.create_table(
        "certificates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("supplier", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "registered_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "processing_status",
            sa.Text(),
            nullable=False,
            server_default="UPLOADED",
        ),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Один и тот же номер у одного поставщика — это один сертификат. Повторная
    # регистрация должна быть конфликтом, а не вторым экземпляром в реестре.
    op.create_unique_constraint(
        "uq_certificate_number", "certificates", ["supplier", "number"]
    )
    op.create_check_constraint(
        "ck_certificate_processing_status",
        "certificates",
        "processing_status IN ('UPLOADED', 'PROCESSING', 'READY', 'FAILED')",
    )
    op.create_index("ix_certificate_supplier", "certificates", ["supplier"])
    op.create_index("ix_certificate_product", "certificates", ["product"])
    op.create_index("ix_certificate_valid_until", "certificates", ["valid_until"])
    op.execute(
        "CREATE TRIGGER set_updated_at BEFORE UPDATE ON certificates"
        " FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at()"
    )
    op.execute(
        "COMMENT ON TABLE certificates IS"
        " 'Реестр сертификатов «поставщик → товар» как источников ТХ."
        " Статус выводится из valid_until, файл лежит документом.'"
    )


def downgrade() -> None:
    op.drop_table("certificates")
    # Возврат NOT NULL возможен, только если документов вне проверок не осталось.
    op.execute("DELETE FROM documents WHERE check_item_id IS NULL")
    op.alter_column("documents", "check_item_id", nullable=False)
