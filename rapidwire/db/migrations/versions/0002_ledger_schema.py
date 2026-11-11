"""Ledger schema: accounts, currencies, balances, executions, transfers, allowances, claims, stakes, outbox, API tokens, permissions and job locks; seeds the stake custody account.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("account_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=True),
        sa.Column("pool_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "(kind = 'user' AND account_id > 0 AND currency_id IS NULL AND pool_id IS NULL) OR (kind = 'issuance' AND account_id < -1 AND currency_id IS NOT NULL AND pool_id IS NULL) OR (kind = 'stake_custody' AND account_id = -1 AND currency_id IS NULL AND pool_id IS NULL) OR (kind = 'pool_custody' AND account_id < -1 AND currency_id IS NULL AND pool_id IS NOT NULL)",
            name=op.f("ck_account_kind_shape"),
        ),
        sa.CheckConstraint("kind IN ('user', 'issuance', 'stake_custody', 'pool_custody')", name=op.f("ck_account_kind")),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_account")),
        sa.UniqueConstraint("account_id", "currency_id", name=op.f("uq_account_account_id_currency_id")),
        sa.UniqueConstraint("currency_id", name=op.f("uq_account_currency_id")),
        sa.UniqueConstraint("pool_id", name=op.f("uq_account_pool_id")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "job_lock",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
        sa.Column("acquired_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_job_lock")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "system_account_allocation",
        sa.Column("allocation_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("allocation_id", name=op.f("pk_system_account_allocation")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_table(
        "api_token",
        sa.Column("api_token_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_used_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_api_token_account_id_account")),
        sa.PrimaryKeyConstraint("api_token_id", name=op.f("pk_api_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_token_token_hash")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_api_token_account_id"), "api_token", ["account_id"], unique=False)
    op.create_table(
        "currency",
        sa.Column("currency_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=24), nullable=False),
        sa.Column("symbol", sa.String(length=8), nullable=False),
        sa.Column("active_symbol", sa.String(length=8), sa.Computed("(CASE WHEN deleted_at IS NULL THEN symbol END)", persisted=True), nullable=True),
        sa.Column("issuance_account_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("minting_renounced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("hourly_rate_ppm", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pending_rate_ppm", sa.Integer(), nullable=True),
        sa.Column("rate_change_requested_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("delete_requested_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("delegation_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("interest_halted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "REGEXP_LIKE(name, '^[a-zA-Z]([a-zA-Z0-9_]*[a-zA-Z0-9])?$', 'c') AND LOCATE('__', name) = 0 AND CHAR_LENGTH(name) - CHAR_LENGTH(REPLACE(name, '_', '')) <= 5",
            name=op.f("ck_currency_name_format"),
        ),
        sa.CheckConstraint("REGEXP_LIKE(symbol, '^[A-Z]+(-[A-Z]+){0,2}$', 'c')", name=op.f("ck_currency_symbol_format")),
        sa.CheckConstraint("(pending_rate_ppm IS NULL) = (rate_change_requested_at IS NULL)", name=op.f("ck_currency_pending_rate_pair")),
        sa.CheckConstraint("currency_id > 0", name=op.f("ck_currency_currency_id_positive")),
        sa.CheckConstraint("hourly_rate_ppm BETWEEN 0 AND 1000000", name=op.f("ck_currency_hourly_rate_range")),
        sa.CheckConstraint("pending_rate_ppm IS NULL OR pending_rate_ppm BETWEEN 0 AND 1000000", name=op.f("ck_currency_pending_rate_range")),
        sa.ForeignKeyConstraint(["created_by"], ["account.account_id"], name=op.f("fk_currency_created_by_account")),
        sa.ForeignKeyConstraint(["issuance_account_id", "currency_id"], ["account.account_id", "account.currency_id"], name=op.f("fk_currency_issuance_account")),
        sa.PrimaryKeyConstraint("currency_id", name=op.f("pk_currency")),
        sa.UniqueConstraint("active_symbol", name=op.f("uq_currency_active_symbol")),
        sa.UniqueConstraint("issuance_account_id", name=op.f("uq_currency_issuance_account_id")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_currency_created_by"), "currency", ["created_by"], unique=False)
    op.create_index(op.f("ix_currency_issuance_account_id_currency_id"), "currency", ["issuance_account_id", "currency_id"], unique=False)
    op.create_table(
        "discord_permission",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_discord_permission_account_id_account")),
        sa.ForeignKeyConstraint(["granted_by"], ["account.account_id"], name=op.f("fk_discord_permission_granted_by_account")),
        sa.PrimaryKeyConstraint("guild_id", "account_id", name=op.f("pk_discord_permission")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_discord_permission_account_id"), "discord_permission", ["account_id"], unique=False)
    op.create_index(op.f("ix_discord_permission_granted_by"), "discord_permission", ["granted_by"], unique=False)
    op.create_table(
        "execution",
        sa.Column("execution_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("initiator_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_execution_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("contract_function", sa.String(length=64), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("status IN ('success', 'failed', 'reverted')", name=op.f("ck_execution_status")),
        sa.ForeignKeyConstraint(["initiator_id"], ["account.account_id"], name=op.f("fk_execution_initiator_id_account")),
        sa.ForeignKeyConstraint(["parent_execution_id"], ["execution.execution_id"], name=op.f("fk_execution_parent_execution_id_execution")),
        sa.PrimaryKeyConstraint("execution_id", name=op.f("pk_execution")),
        sa.UniqueConstraint("initiator_id", "idempotency_key", name=op.f("uq_execution_initiator_id_idempotency_key")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_execution_contract_function_execution_id"), "execution", ["contract_function", "execution_id"], unique=False)
    op.create_index(op.f("ix_execution_created_at"), "execution", ["created_at"], unique=False)
    op.create_index(op.f("ix_execution_initiator_id_execution_id"), "execution", ["initiator_id", "execution_id"], unique=False)
    op.create_index(op.f("ix_execution_parent_execution_id"), "execution", ["parent_execution_id"], unique=False)
    op.create_index(op.f("ix_execution_type_execution_id"), "execution", ["type", "execution_id"], unique=False)
    op.create_table(
        "notification_permission",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("allowed_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("user_id <> allowed_user_id", name=op.f("ck_notification_permission_distinct_users")),
        sa.ForeignKeyConstraint(["allowed_user_id"], ["account.account_id"], name=op.f("fk_notification_permission_allowed_user_id_account")),
        sa.ForeignKeyConstraint(["user_id"], ["account.account_id"], name=op.f("fk_notification_permission_user_id_account")),
        sa.PrimaryKeyConstraint("user_id", "allowed_user_id", name=op.f("pk_notification_permission")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_notification_permission_allowed_user_id"), "notification_permission", ["allowed_user_id"], unique=False)
    op.create_table(
        "allowance",
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("spender_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_allowance_amount_non_negative")),
        sa.CheckConstraint("owner_id <> spender_id", name=op.f("ck_allowance_distinct_parties")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_allowance_currency_id_currency")),
        sa.ForeignKeyConstraint(["owner_id"], ["account.account_id"], name=op.f("fk_allowance_owner_id_account")),
        sa.ForeignKeyConstraint(["spender_id"], ["account.account_id"], name=op.f("fk_allowance_spender_id_account")),
        sa.PrimaryKeyConstraint("owner_id", "spender_id", "currency_id", name=op.f("pk_allowance")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_allowance_currency_id"), "allowance", ["currency_id"], unique=False)
    op.create_index(op.f("ix_allowance_spender_id"), "allowance", ["spender_id"], unique=False)
    op.create_table(
        "allowance_history",
        sa.Column("allowance_history_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("spender_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("old_amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("new_amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("old_amount >= 0 AND new_amount >= 0", name=op.f("ck_allowance_history_amounts_non_negative")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_allowance_history_currency_id_currency")),
        sa.ForeignKeyConstraint(["execution_id"], ["execution.execution_id"], name=op.f("fk_allowance_history_execution_id_execution")),
        sa.ForeignKeyConstraint(["owner_id"], ["account.account_id"], name=op.f("fk_allowance_history_owner_id_account")),
        sa.ForeignKeyConstraint(["spender_id"], ["account.account_id"], name=op.f("fk_allowance_history_spender_id_account")),
        sa.PrimaryKeyConstraint("allowance_history_id", name=op.f("pk_allowance_history")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_allowance_history_currency_id"), "allowance_history", ["currency_id"], unique=False)
    op.create_index(op.f("ix_allowance_history_execution_id"), "allowance_history", ["execution_id"], unique=False)
    op.create_index(op.f("ix_allowance_history_owner_id_spender_id_currency_id"), "allowance_history", ["owner_id", "spender_id", "currency_id"], unique=False)
    op.create_index(op.f("ix_allowance_history_spender_id"), "allowance_history", ["spender_id"], unique=False)
    op.create_table(
        "balance",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("allow_negative", sa.Boolean(), nullable=False),
        sa.Column("negative_currency_id", sa.BigInteger(), sa.Computed("(IF(allow_negative, currency_id, NULL))", persisted=True), nullable=True),
        sa.CheckConstraint("amount >= 0 OR allow_negative", name=op.f("ck_balance_non_negative")),
        sa.ForeignKeyConstraint(["account_id", "negative_currency_id"], ["account.account_id", "account.currency_id"], name=op.f("fk_balance_negative_issuance")),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_balance_account_id_account")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_balance_currency_id_currency")),
        sa.PrimaryKeyConstraint("account_id", "currency_id", name=op.f("pk_balance")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_balance_account_id_negative_currency_id"), "balance", ["account_id", "negative_currency_id"], unique=False)
    op.create_index(op.f("ix_balance_currency_id"), "balance", ["currency_id"], unique=False)
    op.create_table(
        "claim",
        sa.Column("claim_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("claimant_id", sa.BigInteger(), nullable=False),
        sa.Column("payer_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("description", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("resolved_execution_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("(status = 'pending') = (resolved_execution_id IS NULL)", name=op.f("ck_claim_resolution")),
        sa.CheckConstraint("status IN ('pending', 'paid', 'canceled')", name=op.f("ck_claim_status")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_claim_amount_positive")),
        sa.ForeignKeyConstraint(["claimant_id"], ["account.account_id"], name=op.f("fk_claim_claimant_id_account")),
        sa.ForeignKeyConstraint(["created_execution_id"], ["execution.execution_id"], name=op.f("fk_claim_created_execution_id_execution")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_claim_currency_id_currency")),
        sa.ForeignKeyConstraint(["payer_id"], ["account.account_id"], name=op.f("fk_claim_payer_id_account")),
        sa.ForeignKeyConstraint(["resolved_execution_id"], ["execution.execution_id"], name=op.f("fk_claim_resolved_execution_id_execution")),
        sa.PrimaryKeyConstraint("claim_id", name=op.f("pk_claim")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_claim_claimant_id_claim_id"), "claim", ["claimant_id", "claim_id"], unique=False)
    op.create_index(op.f("ix_claim_created_execution_id"), "claim", ["created_execution_id"], unique=False)
    op.create_index(op.f("ix_claim_currency_id_status"), "claim", ["currency_id", "status"], unique=False)
    op.create_index(op.f("ix_claim_payer_id_claim_id"), "claim", ["payer_id", "claim_id"], unique=False)
    op.create_index(op.f("ix_claim_resolved_execution_id"), "claim", ["resolved_execution_id"], unique=False)
    op.create_table(
        "outbox",
        sa.Column("outbox_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("sent_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'sent', 'failed')", name=op.f("ck_outbox_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_attempts_non_negative")),
        sa.ForeignKeyConstraint(["execution_id"], ["execution.execution_id"], name=op.f("fk_outbox_execution_id_execution")),
        sa.PrimaryKeyConstraint("outbox_id", name=op.f("pk_outbox")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_outbox_execution_id"), "outbox", ["execution_id"], unique=False)
    op.create_index(op.f("ix_outbox_status_available_at"), "outbox", ["status", "available_at"], unique=False)
    op.create_table(
        "stake",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("carry", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_accrued_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_stake_amount_non_negative")),
        sa.CheckConstraint("carry >= 0 AND carry < 1000000000000000000", name=op.f("ck_stake_carry_range")),
        sa.ForeignKeyConstraint(["account_id"], ["account.account_id"], name=op.f("fk_stake_account_id_account")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_stake_currency_id_currency")),
        sa.PrimaryKeyConstraint("account_id", "currency_id", name=op.f("pk_stake")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_stake_currency_id_last_accrued_at"), "stake", ["currency_id", "last_accrued_at"], unique=False)
    op.create_table(
        "transfer",
        sa.Column("transfer_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("from_account", sa.BigInteger(), nullable=False),
        sa.Column("to_account", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transfer_amount_positive")),
        sa.CheckConstraint("from_account <> to_account", name=op.f("ck_transfer_distinct_accounts")),
        sa.ForeignKeyConstraint(["currency_id"], ["currency.currency_id"], name=op.f("fk_transfer_currency_id_currency")),
        sa.ForeignKeyConstraint(["execution_id"], ["execution.execution_id"], name=op.f("fk_transfer_execution_id_execution")),
        sa.ForeignKeyConstraint(["from_account"], ["account.account_id"], name=op.f("fk_transfer_from_account_account")),
        sa.ForeignKeyConstraint(["to_account"], ["account.account_id"], name=op.f("fk_transfer_to_account_account")),
        sa.PrimaryKeyConstraint("transfer_id", name=op.f("pk_transfer")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_bin",
        mysql_engine="InnoDB",
    )
    op.create_index(op.f("ix_transfer_amount"), "transfer", ["amount"], unique=False)
    op.create_index(op.f("ix_transfer_created_at"), "transfer", ["created_at"], unique=False)
    op.create_index(op.f("ix_transfer_currency_id_transfer_id"), "transfer", ["currency_id", "transfer_id"], unique=False)
    op.create_index(op.f("ix_transfer_execution_id"), "transfer", ["execution_id"], unique=False)
    op.create_index(op.f("ix_transfer_from_account_transfer_id"), "transfer", ["from_account", "transfer_id"], unique=False)
    op.create_index(op.f("ix_transfer_to_account_transfer_id"), "transfer", ["to_account", "transfer_id"], unique=False)
    op.create_index(op.f("ix_transfer_from_account_created_at"), "transfer", ["from_account", "created_at"], unique=False)
    op.create_index(op.f("ix_transfer_to_account_created_at"), "transfer", ["to_account", "created_at"], unique=False)
    op.execute("INSERT INTO account (account_id, kind, currency_id, pool_id, created_at) VALUES (-1, 'stake_custody', NULL, NULL, UTC_TIMESTAMP(6))")


def downgrade() -> None:
    op.drop_index(op.f("ix_transfer_to_account_created_at"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_from_account_created_at"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_to_account_transfer_id"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_from_account_transfer_id"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_execution_id"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_currency_id_transfer_id"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_created_at"), table_name="transfer")
    op.drop_index(op.f("ix_transfer_amount"), table_name="transfer")
    op.drop_table("transfer")
    op.drop_index(op.f("ix_stake_currency_id_last_accrued_at"), table_name="stake")
    op.drop_table("stake")
    op.drop_index(op.f("ix_outbox_status_available_at"), table_name="outbox")
    op.drop_index(op.f("ix_outbox_execution_id"), table_name="outbox")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_claim_resolved_execution_id"), table_name="claim")
    op.drop_index(op.f("ix_claim_payer_id_claim_id"), table_name="claim")
    op.drop_index(op.f("ix_claim_currency_id_status"), table_name="claim")
    op.drop_index(op.f("ix_claim_created_execution_id"), table_name="claim")
    op.drop_index(op.f("ix_claim_claimant_id_claim_id"), table_name="claim")
    op.drop_table("claim")
    op.drop_index(op.f("ix_balance_currency_id"), table_name="balance")
    op.drop_index(op.f("ix_balance_account_id_negative_currency_id"), table_name="balance")
    op.drop_table("balance")
    op.drop_index(op.f("ix_allowance_history_spender_id"), table_name="allowance_history")
    op.drop_index(op.f("ix_allowance_history_owner_id_spender_id_currency_id"), table_name="allowance_history")
    op.drop_index(op.f("ix_allowance_history_execution_id"), table_name="allowance_history")
    op.drop_index(op.f("ix_allowance_history_currency_id"), table_name="allowance_history")
    op.drop_table("allowance_history")
    op.drop_index(op.f("ix_allowance_spender_id"), table_name="allowance")
    op.drop_index(op.f("ix_allowance_currency_id"), table_name="allowance")
    op.drop_table("allowance")
    op.drop_index(op.f("ix_notification_permission_allowed_user_id"), table_name="notification_permission")
    op.drop_table("notification_permission")
    op.drop_index(op.f("ix_execution_type_execution_id"), table_name="execution")
    op.drop_index(op.f("ix_execution_parent_execution_id"), table_name="execution")
    op.drop_index(op.f("ix_execution_initiator_id_execution_id"), table_name="execution")
    op.drop_index(op.f("ix_execution_created_at"), table_name="execution")
    op.drop_index(op.f("ix_execution_contract_function_execution_id"), table_name="execution")
    op.drop_table("execution")
    op.drop_index(op.f("ix_discord_permission_granted_by"), table_name="discord_permission")
    op.drop_index(op.f("ix_discord_permission_account_id"), table_name="discord_permission")
    op.drop_table("discord_permission")
    op.drop_index(op.f("ix_currency_issuance_account_id_currency_id"), table_name="currency")
    op.drop_index(op.f("ix_currency_created_by"), table_name="currency")
    op.drop_table("currency")
    op.drop_index(op.f("ix_api_token_account_id"), table_name="api_token")
    op.drop_table("api_token")
    op.drop_table("system_account_allocation")
    op.drop_table("job_lock")
    op.drop_table("account")
