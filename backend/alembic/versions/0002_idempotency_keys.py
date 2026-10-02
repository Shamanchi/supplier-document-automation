"""Хранилище Idempotency-Key.

Revision ID: 0002_idempotency_keys
Revises: 0001_initial_schema
Create Date: 2026-08-11

Таблица описана решением P1-11 (§18.7 PHASE3_CANONICAL_ARCHITECTURE.md), но в
`db/schema.sql` не попала: тот файл описывает доменный контур Фазы 3 (46 таблиц,
покрытых `db/schema_invariants_test.sql`), а это инфраструктура транспортного
слоя. Поэтому таблица создаётся отдельной миграцией и обычными операциями
Alembic — plpgsql и составных ограничений здесь нет.

Отличие от §18.7: `response_status`/`response_body` объявлены NULLABLE. Спека
описывает их NOT NULL, но тогда запись можно вставить только ПОСЛЕ выполнения
операции, и два одновременных запроса с одним ключом успевают выполниться оба —
ровно то, от чего ключ защищает. Nullable-колонки позволяют сначала занять ключ
(INSERT ... ON CONFLICT DO NOTHING), выполнить операцию и лишь затем записать
ответ; занятый, но ещё не завершённый ключ виден как `response_status IS NULL`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_idempotency_keys"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(255), primary_key=True),
        # SHA-256 от метода, пути и тела запроса: повтор с тем же ключом, но
        # другим телом — ошибка клиента, а не повтор операции.
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Retention-job удаляет истёкшие ключи (TTL 24 часа) — ему нужен этот индекс.
    op.create_index("ix_idempotency_expires", "idempotency_keys", ["expires_at"])
    op.execute(
        "COMMENT ON TABLE idempotency_keys IS"
        " 'Ответы мутирующих операций по Idempotency-Key; TTL 24 часа (§18.7).'"
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_expires", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
