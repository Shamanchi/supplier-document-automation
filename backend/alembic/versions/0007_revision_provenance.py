"""Провенанс ручной правки: прежнее значение и поколение evidence.

Revision ID: 0007_revision_provenance
Revises: 0006_pipeline_execution
Create Date: 2026-08-29

`parameter_revisions` хранила только новое значение, а `raw_parameters`
правилась на месте — исходное извлечённое значение после правки было
невосстановимо, хотя канон требует «ручная правка создаёт ревизию, оригинал не
удаляется». Добавляются `previous_*`: ревизия становится полной записью
перехода «было → стало».

`extraction_evidence` неизменяема (триггер `prevent_modify`), поэтому пометить
устаревшую цитату нельзя — вместо этого у значения появляется поколение:

* `raw_parameters.value_revision` растёт **только** при изменении значения
  (ручная правка), в отличие от `version`, который растёт и при исключении или
  возврате параметра ради optimistic lock;
* `extraction_evidence.raw_parameter_value_revision` фиксирует, какое поколение
  значения подтверждает цитата.

Так инвариант «value_text ⊂ quote» проверяем: актуальные цитаты — те, у
которых поколение совпадает с параметром, а машинная цитата исходного значения
остаётся в истории и не выдаётся за подтверждение правленого.

`ADD COLUMN` с константным `server_default` в PostgreSQL 11+ не переписывает
строки и не поднимает построчные триггеры, поэтому неизменяемые таблицы
переживают миграцию.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_revision_provenance"
down_revision = "0006_pipeline_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("previous_name_text", "previous_value_text", "previous_unit_text"):
        op.add_column("parameter_revisions", sa.Column(column, sa.Text(), nullable=True))
    op.execute(
        "COMMENT ON COLUMN parameter_revisions.previous_value_text IS"
        " 'Значение до правки; вместе с previous_name_text и previous_unit_text"
        " делает ревизию полной записью перехода.'"
    )

    op.add_column(
        "raw_parameters",
        sa.Column(
            "value_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        "COMMENT ON COLUMN raw_parameters.value_revision IS"
        " 'Поколение значения: растёт только при ручной правке, в отличие от"
        " version, который растёт и при exclude/restore.'"
    )

    op.add_column(
        "extraction_evidence",
        sa.Column(
            "raw_parameter_value_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.execute(
        "COMMENT ON COLUMN extraction_evidence.raw_parameter_value_revision IS"
        " 'Поколение значения, которое подтверждает цитата. Совпадает с"
        " raw_parameters.value_revision у актуального evidence.'"
    )


def downgrade() -> None:
    op.drop_column("extraction_evidence", "raw_parameter_value_revision")
    op.drop_column("raw_parameters", "value_revision")
    for column in ("previous_unit_text", "previous_value_text", "previous_name_text"):
        op.drop_column("parameter_revisions", column)
