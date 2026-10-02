"""Решение менеджера по вердикту заявки.

Revision ID: 0008_batch_verdict
Revises: 0007_revision_provenance
Create Date: 2026-08-30

`check_batches.final_verdict` схема описывает как «агрегат по позициям, который
пересчитывает модуль verdicts», но места под **решение человека** по заявке в
ней нет: `item_verdicts` привязан к позиции, а отдельной таблицы вердиктов
заявки не заведено. Пока колонок не было, `final_verdict` выставлялся `PENDING`
при создании и больше не менялся — `GET /check-batches/{id}/summary` отдавал
его как расчётный вердикт, то есть всегда врал.

Колонки повторяют форму `item_verdicts`, чтобы контур заявки и контур позиции
читались одинаково:

* `final_verdict` (уже был) — расчёт по вердиктам позиций;
* `manager_verdict` — решение менеджера, отдельно от расчёта;
* `verdict_basis` — распределение вердиктов позиций, из чего сложился расчёт;
* `verdict_comment` — обязателен при расхождении с расчётом;
* `verdict_approved_by` / `verdict_approved_at` — кто и когда подтвердил;
* `verdict_version` — свой optimistic lock. Отдельно от `check_batches.version`
  намеренно: тот отвечает за состав и метаданные заявки, и пересчёт вердикта не
  должен ронять `If-Match` у клиента, который редактирует заявку.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_batch_verdict"
down_revision = "0007_revision_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "check_batches",
        sa.Column(
            "manager_verdict",
            postgresql.ENUM(name="verdict_value", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "check_batches", sa.Column("verdict_basis", postgresql.JSONB(), nullable=True)
    )
    op.add_column("check_batches", sa.Column("verdict_comment", sa.Text(), nullable=True))
    op.add_column(
        "check_batches",
        sa.Column(
            "verdict_approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "check_batches",
        sa.Column("verdict_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "check_batches",
        sa.Column(
            "verdict_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        "COMMENT ON COLUMN check_batches.manager_verdict IS"
        " 'Решение менеджера по заявке; final_verdict остаётся расчётом.'"
    )
    op.execute(
        "COMMENT ON COLUMN check_batches.verdict_version IS"
        " 'Optimistic lock вердикта заявки, отдельно от version самой заявки.'"
    )


def downgrade() -> None:
    for column in (
        "verdict_version",
        "verdict_approved_at",
        "verdict_approved_by",
        "verdict_comment",
        "verdict_basis",
        "manager_verdict",
    ):
        op.drop_column("check_batches", column)
