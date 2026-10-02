"""Связующая миграция шага 05: identity фазы 2 уступает место схеме фазы 3.

Revision ID: p3_bridge_identity_merge
Revises: n1_merge_vat_durable_rule
Create Date: 2026-09-05

docs/migration/05-migrations.md: между головой графа фазы 2
(n1_merge_vat_durable_rule) и 0001_initial_schema фазы 3 вставляется мост,
который приводит 4 общие identity-таблицы к виду, ожидаемому P3-DDL.

Почему DROP, а не ALTER-конвертация из плана шага 02 (users.status → enum и
т.д.): миграция 0001_initial_schema фазы 3 остаётся byte-идентичной (правило
плана — проверенный DDL не переписывается), а она сама исполняет
``CREATE TABLE users/user_roles/refresh_sessions`` и ``CREATE TYPE
user_role/user_status`` — то есть к моменту её запуска этих таблиц и типов в
БД быть не должно. Роль ``role_permissions`` пересоздаёт 0003 (тоже
CREATE TABLE), поэтому она убирается здесь же.

Данные не сохраняются осознанно: в БД фазы 2 это 4 dev-аккаунта
``<role>@docprice.internal`` и 24 refresh-сессии, созданные bootstrap'ом.
Bootstrap приложения пересоздаёт их при старте (P2-аккаунты admin/user/
sysadmin/viewer — при пустой таблице users, что как раз требуется критерию
приёмки шага 02 «вход старым логином фазы 2»); сессии устаревают —
пользователям просто придётся перелогиниться.

Downgrade возвращает таблицы в исходный вид миграции g1_identity_tables
(String-колонки), чтобы откат до n1_merge_vat_durable_rule оставлял
консистентную P2-схему. Наполнение данными downgrade не восстанавливает —
аккаунты пересоздаются bootstrap'ом.
"""

import sqlalchemy as sa
from alembic import op

revision = "p3_bridge_identity_merge"
down_revision = "d9e6c2b1a4f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Порядок — по FK: сначала дочерние таблицы, users последней.
    # Данные users/user_roles/refresh_sessions — dev-bootstrap (см. docstring),
    # role_permissions заполняется заново миграцией 0003 фазы 3.
    op.drop_table("refresh_sessions")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("users")


def downgrade() -> None:
    # Исходный вид миграции g1_identity_tables (String-колонки фазы 2).
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "user_roles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.String(32), nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("permission", sa.String(160), primary_key=True),
    )
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash"),
    )
