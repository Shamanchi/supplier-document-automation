"""Хранилище RBAC-конфигурации «роль → права».

Revision ID: 0003_role_permissions
Revises: 0002_idempotency_keys
Create Date: 2026-08-11

`PATCH /profile/permissions` (docs/api/profile/README.md) заменяет набор прав
роли, но в `db/schema.sql` таблицы для этого нет: `roles` из дампа Фазы 2 не
переносилась (роли стали enum `user_role`, см. CONTOUR_BOUNDARIES.md §4.3), а
пары «роль → право» не было ни там, ни здесь. До этой миграции эндпоинт отвечал
`501`, потому что сохранять правку было некуда.

Права хранятся отдельными строками, а не массивом: так `PRIMARY KEY (role,
permission)` сам запрещает дубликаты, а выдача прав роли — обычный `SELECT`
без разбора массива.

Значения прав — те же строки, что показывает экран профиля. Машинных кодов в
контракте нет; введение их затронуло бы и фронтенд, поэтому строки оставлены
как есть, а каталог допустимых значений задан в коде (`identity/permissions.py`).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_role_permissions"
down_revision = "0002_idempotency_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_permissions",
        sa.Column(
            "role",
            postgresql.ENUM(name="user_role", create_type=False),
            primary_key=True,
        ),
        sa.Column("permission", sa.Text(), primary_key=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "COMMENT ON TABLE role_permissions IS"
        " 'RBAC-конфигурация: какие права закреплены за ролью."
        " Связка «пользователь → роль» — в user_roles.'"
    )


def downgrade() -> None:
    op.drop_table("role_permissions")
