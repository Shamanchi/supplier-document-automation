"""Решения человека по документам: подтверждение, отклонение, исправление номера.

Сверка с реестром отвечает на вопрос «что говорит реестр», но приложить
документ к коммерческому предложению — ответственность человека. Решение
хранится отдельной таблицей, append-only, вместе с состоянием, на котором
оно принято.

Revision ID: 0004_fsa_decisions
Revises: 0003_fsa_imports
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_fsa_decisions"
down_revision = "0003_fsa_imports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_decision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "check_id",
            sa.Uuid(),
            sa.ForeignKey("document_check.id", ondelete="SET NULL"),
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False, server_default=""),
        sa.Column("comment", sa.Text(), server_default=""),
        sa.Column("verdict_at_decision", sa.String(length=16), server_default=""),
        sa.Column("number_at_decision", sa.String(length=128), server_default=""),
        sa.Column("recognized_at_decision", sa.DateTime(timezone=True)),
        sa.Column("corrected_number", sa.Text(), server_default=""),
        sa.Column("corrected_number_key", sa.String(length=128), server_default=""),
        sa.Column("corrected_verdict", sa.String(length=16), server_default=""),
        sa.Column("corrected_valid_until", sa.Date()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_document_decision_document", "document_decision", ["document_id", "created_at"]
    )
    op.create_index("ix_document_decision_action", "document_decision", ["action"])


def downgrade() -> None:
    op.drop_index("ix_document_decision_action", table_name="document_decision")
    op.drop_index("ix_document_decision_document", table_name="document_decision")
    op.drop_table("document_decision")
