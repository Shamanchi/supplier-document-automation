"""merge vat and identity heads

Revision ID: f75cc0d31513
Revises: b857bef6c213, m1_merge_identity_domain
Create Date: 2026-08-27
"""

from typing import Sequence, Union

revision: str = "f75cc0d31513"
down_revision: Union[str, Sequence[str], None] = ("b857bef6c213", "m1_merge_identity_domain")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
