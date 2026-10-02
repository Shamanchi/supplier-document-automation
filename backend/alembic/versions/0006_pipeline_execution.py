"""Исполнение прогонов: журнал событий, лизинг воркера и запрос отмены.

Revision ID: 0006_pipeline_execution
Revises: 0005_certificate_extraction
Create Date: 2026-08-29

`pipeline_runs` и `stage_runs` из `db/schema.sql` описывают состояние прогона,
но не его историю: `stage_runs` хранит только текущее значение этапа. Контракт
check-items §SSE требует монотонный `id` события и переживание reconnect —
клиент присылает `Last-Event-ID` и должен получить всё, что пропустил. Из
состояния такую историю не восстановить, поэтому добавляется журнал
`pipeline_events` с BIGSERIAL: он и источник SSE, и след аудита.

`pipeline_runs` получает три колонки под исполнение:

* `worker_token` + `lease_expires_at` — лизинг, как у `extraction_runs`: второй
  воркер не подхватит прогон, пока лизинг жив, а после истечения прогон
  подхватывается снова, и «повисший» воркер уже не сможет записать результат;
* `cancel_requested_at` — отмена по контракту асинхронная (`202
  CANCEL_REQUESTED`, «worker проверяет флаг между страницами/батчами»), поэтому
  это именно запрос отмены, а не статус: перевод в `CANCELLED` делает воркер.

Расхождение с `db/schema.sql` отмечено в docs/todo/pipeline.md.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_pipeline_execution"
down_revision = "0005_certificate_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipeline_runs", sa.Column("worker_token", sa.Text(), nullable=True))
    op.add_column(
        "pipeline_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "COMMENT ON COLUMN pipeline_runs.cancel_requested_at IS"
        " 'Запрос отмены; перевод прогона в CANCELLED делает worker между этапами.'"
    )

    op.create_table(
        "pipeline_events",
        # BIGSERIAL, а не UUID: контракт SSE требует монотонный курсор, по
        # которому клиент после обрыва добирает пропущенное одним диапазоном.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "check_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("check_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pipeline_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("progress_unit", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Основной запрос SSE: «события позиции после курсора, по возрастанию id».
    op.create_index(
        "ix_pipeline_events_item",
        "pipeline_events",
        ["check_item_id", "id"],
    )
    op.create_index(
        "ix_pipeline_events_run",
        "pipeline_events",
        ["pipeline_run_id", "id"],
    )
    op.execute(
        "COMMENT ON TABLE pipeline_events IS"
        " 'Журнал событий прогона: источник SSE и след аудита. Событие"
        " неизменяемо, курсор клиента — id.'"
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_run", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_item", table_name="pipeline_events")
    op.drop_table("pipeline_events")
    op.drop_column("pipeline_runs", "cancel_requested_at")
    op.drop_column("pipeline_runs", "lease_expires_at")
    op.drop_column("pipeline_runs", "worker_token")
