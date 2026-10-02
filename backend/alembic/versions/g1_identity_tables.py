"""Add shared Phase 3 identity tables."""
from alembic import op
import sqlalchemy as sa

revision = "g1_identity_tables"
down_revision = "f3a7d9e2c4b1"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("users", sa.Column("id", sa.UUID(), primary_key=True), sa.Column("email", sa.String(320), nullable=False), sa.Column("display_name", sa.String(255), nullable=False), sa.Column("password_hash", sa.Text()), sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.UniqueConstraint("email"))
    op.create_table("user_roles", sa.Column("id", sa.UUID(), primary_key=True), sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("role", sa.String(32), nullable=False))
    op.create_table("role_permissions", sa.Column("role", sa.String(32), primary_key=True), sa.Column("permission", sa.String(160), primary_key=True))
    op.create_table("refresh_sessions", sa.Column("id", sa.UUID(), primary_key=True), sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False), sa.Column("family_id", sa.UUID(), nullable=False), sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("token_hash"))

def downgrade() -> None:
    op.drop_table("refresh_sessions")
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("users")
