"""DEX schema: pools and liquidity provider shares, and the foreign key from pool custody accounts to their pool.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pool",
        sa.Column("pool_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("currency_a", sa.BigInteger(), nullable=False),
        sa.Column("currency_b", sa.BigInteger(), nullable=False),
        sa.Column("fee_bps", sa.Integer(), nullable=False),
        sa.Column("reserve_a", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("reserve_b", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("total_shares", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("locked_shares", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("dissolved_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("dissolved_execution_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), sa.Computed("(CASE WHEN dissolved_at IS NULL THEN 1 END)", persisted=True), nullable=True),
        sa.CheckConstraint("currency_a < currency_b", name=op.f("ck_pool_currency_order")),
        sa.CheckConstraint("fee_bps IN (100, 50, 10)", name=op.f("ck_pool_fee_tier")),
        sa.CheckConstraint("reserve_a >= 0", name=op.f("ck_pool_reserve_a_non_negative")),
        sa.CheckConstraint("reserve_b >= 0", name=op.f("ck_pool_reserve_b_non_negative")),
        sa.CheckConstraint("total_shares >= 0", name=op.f("ck_pool_total_shares_non_negative")),
        sa.CheckConstraint("locked_shares >= 0", name=op.f("ck_pool_locked_shares_non_negative")),
        sa.CheckConstraint("(dissolved_at IS NULL) = (dissolved_execution_id IS NULL)", name=op.f("ck_pool_dissolution_pair")),
        sa.CheckConstraint(
            "(dissolved_at IS NULL AND reserve_a > 0 AND reserve_b > 0 AND locked_shares = 1000 AND total_shares >= locked_shares) OR (dissolved_at IS NOT NULL AND reserve_a = 0 AND reserve_b = 0 AND total_shares = 0 AND locked_shares = 0)",
            name=op.f("ck_pool_state"),
        ),
        sa.ForeignKeyConstraint(["currency_a"], ["currency.currency_id"], name=op.f("fk_pool_currency_a_currency")),
        sa.ForeignKeyConstraint(["currency_b"], ["currency.currency_id"], name=op.f("fk_pool_currency_b_currency")),
        sa.ForeignKeyConstraint(["created_by"], ["account.account_id"], name=op.f("fk_pool_created_by_account")),
        sa.ForeignKeyConstraint(["created_execution_id"], ["execution.execution_id"], name=op.f("fk_pool_created_execution_id_execution")),
        sa.ForeignKeyConstraint(["dissolved_execution_id"], ["execution.execution_id"], name=op.f("fk_pool_dissolved_execution_id_execution")),
        sa.PrimaryKeyConstraint("pool_id", name=op.f("pk_pool")),
        sa.UniqueConstraint("currency_a", "currency_b", "fee_bps", "active", name=op.f("uq_pool_currency_a_currency_b_fee_bps_active")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_pool_currency_b"), "pool", ["currency_b"], unique=False)
    op.create_index(op.f("ix_pool_active_pool_id"), "pool", ["active", "pool_id"], unique=False)
    op.create_index(op.f("ix_pool_created_by"), "pool", ["created_by"], unique=False)
    op.create_index(op.f("ix_pool_created_execution_id"), "pool", ["created_execution_id"], unique=False)
    op.create_index(op.f("ix_pool_dissolved_execution_id"), "pool", ["dissolved_execution_id"], unique=False)
    op.create_table(
        "pool_share",
        sa.Column("pool_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("shares", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("shares > 0", name=op.f("ck_pool_share_shares_positive")),
        sa.ForeignKeyConstraint(["pool_id"], ["pool.pool_id"], name=op.f("fk_pool_share_pool_id_pool")),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_pool_share_account_id_account")),
        sa.PrimaryKeyConstraint("pool_id", "account_id", name=op.f("pk_pool_share")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_pool_share_account_id_pool_id"), "pool_share", ["account_id", "pool_id"], unique=False)
    op.create_foreign_key(op.f("fk_account_pool_id_pool"), "account", "pool", ["pool_id"], ["pool_id"])


def downgrade() -> None:
    # Indexes go with their tables; MySQL refuses to drop an index that backs a foreign key on its own.
    op.drop_constraint(op.f("fk_account_pool_id_pool"), "account", type_="foreignkey")
    op.drop_table("pool_share")
    op.drop_table("pool")
