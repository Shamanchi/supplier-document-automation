"""Merge the identity and domain migration branches."""

from typing import Sequence, Union

from alembic import op


revision: str = "m1_merge_identity_domain"
down_revision: Union[str, Sequence[str], None] = (
    "g1_identity_tables",
    "c9f2a7e31d04",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
