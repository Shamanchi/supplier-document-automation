"""merge vat and durable rule generation heads

The VAT chain and the durable rule-generation chain both branch off
c9f2a7e31d04 and were developed independently, so integrating them leaves
two heads. Neither touches the other's tables, so the merge is empty.

Revision ID: n1_merge_vat_durable_rule
Revises: f75cc0d31513, e139c2a7f401
Create Date: 2026-08-30
"""

from typing import Sequence, Union

revision: str = "n1_merge_vat_durable_rule"
down_revision: Union[str, Sequence[str], None] = ("f75cc0d31513", "e139c2a7f401")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
