"""Contract schema: contracts, their registered versions and their public storage.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contract_version",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("code", sa.BLOB(), nullable=False),
        sa.Column("sha256", sa.BINARY(length=32), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_contract_version_user_account")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_contract_version_version_positive")),
        sa.CheckConstraint("size BETWEEN 1 AND 8000 AND size = LENGTH(code)", name=op.f("ck_contract_version_size")),
        sa.CheckConstraint("sha256 = UNHEX(SHA2(code, 256))", name=op.f("ck_contract_version_sha256")),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_contract_version_account_id_account")),
        sa.ForeignKeyConstraint(["created_execution_id"], ["execution.execution_id"], name=op.f("fk_contract_version_created_execution_id_execution")),
        sa.PrimaryKeyConstraint("account_id", "version", name=op.f("pk_contract_version")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_contract_version_created_execution_id"), "contract_version", ["created_execution_id"], unique=False)
    op.create_table(
        "contract",
        sa.Column("account_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("locked_until", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("storage_keys", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("storage_bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_contract_user_account")),
        sa.CheckConstraint("status IN ('active', 'paused', 'deleted')", name=op.f("ck_contract_status")),
        sa.CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name=op.f("ck_contract_deletion_pair")),
        sa.CheckConstraint("storage_keys BETWEEN 0 AND 2000", name=op.f("ck_contract_storage_keys_range")),
        sa.CheckConstraint("storage_bytes BETWEEN 0 AND 262144", name=op.f("ck_contract_storage_bytes_range")),
        sa.CheckConstraint("status <> 'deleted' OR (storage_keys = 0 AND storage_bytes = 0 AND locked_until IS NULL)", name=op.f("ck_contract_deleted_state")),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_contract_account_id_account")),
        sa.ForeignKeyConstraint(["account_id", "current_version"], ["contract_version.account_id", "contract_version.version"], name=op.f("fk_contract_current_version")),
        sa.ForeignKeyConstraint(["created_execution_id"], ["execution.execution_id"], name=op.f("fk_contract_created_execution_id_execution")),
        sa.ForeignKeyConstraint(["updated_execution_id"], ["execution.execution_id"], name=op.f("fk_contract_updated_execution_id_execution")),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_contract")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_contract_account_id_current_version"), "contract", ["account_id", "current_version"], unique=False)
    op.create_index(op.f("ix_contract_created_execution_id"), "contract", ["created_execution_id"], unique=False)
    op.create_index(op.f("ix_contract_updated_execution_id"), "contract", ["updated_execution_id"], unique=False)
    op.create_table(
        "contract_storage",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("encoded_value", sa.BLOB(), nullable=False),
        sa.Column("value_size", sa.Integer(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("CHAR_LENGTH(storage_key) >= 1", name=op.f("ck_contract_storage_key_not_empty")),
        sa.CheckConstraint("value_size = LENGTH(encoded_value) AND value_size BETWEEN 1 AND 4096", name=op.f("ck_contract_storage_value_size")),
        sa.ForeignKeyConstraint(["account_id"], ["contract.account_id"], name=op.f("fk_contract_storage_account_id_contract")),
        sa.PrimaryKeyConstraint("account_id", "storage_key", name=op.f("pk_contract_storage")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("contract_storage")
    op.drop_table("contract")
    op.drop_table("contract_version")
