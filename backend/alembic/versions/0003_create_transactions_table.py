"""Create the transactions table.

Revision ID: 0003
Revises: 0002
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create transactions with ownership, card references, and value checks."""
    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.String(length=50), nullable=False),
        sa.Column("type", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_per_unit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "transacted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("type IN ('BUY', 'SELL')", name="ck_transactions_type"),
        sa.CheckConstraint(
            "price_per_unit >= 0", name="ck_transactions_price_nonnegative"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_transactions_quantity_positive"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_card_id", "transactions", ["card_id"], unique=False)
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"], unique=False)


def downgrade() -> None:
    """Remove transaction indexes and the transactions table."""
    op.drop_index("ix_transactions_user_id", table_name="transactions")
    op.drop_index("ix_transactions_card_id", table_name="transactions")
    op.drop_table("transactions")
