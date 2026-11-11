"""Table definitions on the shared metadata, all InnoDB with utf8mb4 and the binary collation; open enumerations (execution type, transfer kind, outbox kind) are VARCHAR without a CHECK, so new members need no migration, and closed ones carry a CHECK listing their members."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.mysql import BINARY, BLOB
from sqlalchemy.sql.naming import conv

from rapidwire.contract.limits import STORAGE_KEY_MAX_LENGTH, STORAGE_VALUE_MAX_BYTES
from rapidwire.core.types import AccountKind, ClaimStatus, ExecutionStatus, OutboxStatus
from rapidwire.db.metadata import metadata
from rapidwire.db.types import Amount, UtcDateTime

__all__ = [
    "CARRY_SCALE",
    "CLAIM_DESCRIPTION_MAX",
    "CONTRACT_FUNCTION_MAX",
    "CONTRACT_MAX_SIZE",
    "CONTRACT_STORAGE_KEY_MAX",
    "CONTRACT_STORAGE_MAX_BYTES",
    "CONTRACT_STORAGE_MAX_KEYS",
    "CONTRACT_STORAGE_VALUE_MAX",
    "CURRENCY_NAME_MAX",
    "CURRENCY_NAME_PATTERN",
    "CURRENCY_NAME_UNDERSCORES_MAX",
    "CURRENCY_SYMBOL_HYPHENS_MAX",
    "CURRENCY_SYMBOL_MAX",
    "CURRENCY_SYMBOL_PATTERN",
    "ENUM_LENGTH",
    "FEE_DENOMINATOR",
    "FEE_TIERS_BPS",
    "GUILD_DIRECTORY_STATE_ID",
    "IDEMPOTENCY_KEY_MAX",
    "JOB_LOCK_HOLDER_MAX",
    "JOB_LOCK_NAME_MAX",
    "MINIMUM_LIQUIDITY",
    "RATE_PPM_MAX",
    "STAKE_CUSTODY_ACCOUNT_ID",
    "TABLE_KWARGS",
    "ContractStatus",
    "account",
    "allowance",
    "allowance_history",
    "api_token",
    "balance",
    "claim",
    "contract",
    "contract_storage",
    "contract_version",
    "currency",
    "discord_permission",
    "execution",
    "guild_administrator_role",
    "guild_directory_state",
    "guild_member_role",
    "guild_member_snapshot",
    "guild_snapshot",
    "job_lock",
    "metadata",
    "notification_permission",
    "outbox",
    "pool",
    "pool_share",
    "stake",
    "system_account_allocation",
    "transfer",
]

TABLE_KWARGS: dict[str, Any] = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}

STAKE_CUSTODY_ACCOUNT_ID = -1
RATE_PPM_MAX = 1_000_000
CARRY_SCALE = 10**18
CLAIM_DESCRIPTION_MAX = 50
CURRENCY_NAME_MAX = 24
CURRENCY_SYMBOL_MAX = 8
CURRENCY_NAME_PATTERN = "^[a-zA-Z]([a-zA-Z0-9_]*[a-zA-Z0-9])?$"
CURRENCY_NAME_UNDERSCORES_MAX = 5
CURRENCY_SYMBOL_HYPHENS_MAX = 2
CURRENCY_SYMBOL_PATTERN = f"^[A-Z]+(-[A-Z]+){{0,{CURRENCY_SYMBOL_HYPHENS_MAX}}}$"
ENUM_LENGTH = 32
CONTRACT_FUNCTION_MAX = 64
IDEMPOTENCY_KEY_MAX = 255
JOB_LOCK_NAME_MAX = 64
JOB_LOCK_HOLDER_MAX = 255
FEE_DENOMINATOR = 10_000
# Pool fee tiers in basis points of FEE_DENOMINATOR: 1%, 0.5% and 0.1%.
FEE_TIERS_BPS = (100, 50, 10)
# Share units locked permanently in a pool on its first deposit.
MINIMUM_LIQUIDITY = 1000
# A storage key is 1 to CONTRACT_STORAGE_KEY_MAX characters and an encoded value 1 to CONTRACT_STORAGE_VALUE_MAX bytes, the VM's own limits.
CONTRACT_STORAGE_KEY_MAX = STORAGE_KEY_MAX_LENGTH
# Largest contract binary in bytes, uncompressed; a smaller limit may be configured but never a larger one.
CONTRACT_MAX_SIZE = 8000
CONTRACT_STORAGE_VALUE_MAX = STORAGE_VALUE_MAX_BYTES
# Per contract at most CONTRACT_STORAGE_MAX_KEYS entries whose accounted sizes (UTF-8 bytes of the key plus bytes of the encoded value) total at most CONTRACT_STORAGE_MAX_BYTES.
CONTRACT_STORAGE_MAX_KEYS = 2000
CONTRACT_STORAGE_MAX_BYTES = 256 * 1024
# The primary key of the only guild_directory_state row.
GUILD_DIRECTORY_STATE_ID = 1


class ContractStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


def _in(column: str, members: Iterable[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in members)
    return f"{column} IN ({values})"


account = Table(
    "account",
    metadata,
    Column("account_id", BigInteger, primary_key=True, autoincrement=False),
    Column("kind", String(ENUM_LENGTH), nullable=False),
    Column("currency_id", BigInteger, nullable=True),
    # The pool row is inserted before its custody account; use_alter breaks the account/pool cycle for table sorting.
    Column("pool_id", BigInteger, ForeignKey("pool.pool_id", use_alter=True), nullable=True),
    Column("created_at", UtcDateTime(), nullable=False),
    CheckConstraint(_in("kind", AccountKind), name="kind"),
    CheckConstraint(
        f"(kind = '{AccountKind.USER}' AND account_id > 0 AND currency_id IS NULL AND pool_id IS NULL)"
        f" OR (kind = '{AccountKind.ISSUANCE}' AND account_id < -1 AND currency_id IS NOT NULL AND pool_id IS NULL)"
        f" OR (kind = '{AccountKind.STAKE_CUSTODY}' AND account_id = {STAKE_CUSTODY_ACCOUNT_ID} AND currency_id IS NULL AND pool_id IS NULL)"
        f" OR (kind = '{AccountKind.POOL_CUSTODY}' AND account_id < -1 AND currency_id IS NULL AND pool_id IS NOT NULL)",
        name="kind_shape",
    ),
    UniqueConstraint("currency_id"),
    UniqueConstraint("pool_id"),
    # The target of the composite foreign keys that tie an issuance account to its own currency.
    UniqueConstraint("account_id", "currency_id"),
    **TABLE_KWARGS,
)

# Issuance and pool custody accounts take the negated AUTO_INCREMENT id of a row here, offset by one so -1 stays reserved for stake custody.
system_account_allocation = Table(
    "system_account_allocation",
    metadata,
    Column("allocation_id", BigInteger, primary_key=True, autoincrement=True),
    **TABLE_KWARGS,
)

# A currency row is never removed: deletion sets deleted_at, and creating the guild's currency again revives the row with new attributes and the same issuance account.
currency = Table(
    "currency",
    metadata,
    Column("currency_id", BigInteger, primary_key=True, autoincrement=False),
    Column("name", String(CURRENCY_NAME_MAX), nullable=False),
    Column("symbol", String(CURRENCY_SYMBOL_MAX), nullable=False),
    Column("active_symbol", String(CURRENCY_SYMBOL_MAX), Computed("(CASE WHEN deleted_at IS NULL THEN symbol END)", persisted=True), nullable=True),
    Column("issuance_account_id", BigInteger, nullable=False),
    Column("created_by", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("minting_renounced", Boolean, nullable=False, server_default=false()),
    Column("hourly_rate_ppm", Integer, nullable=False, server_default=text("0")),
    Column("pending_rate_ppm", Integer, nullable=True),
    Column("rate_change_requested_at", UtcDateTime(), nullable=True),
    Column("delete_requested_at", UtcDateTime(), nullable=True),
    Column("delegation_enabled", Boolean, nullable=False, server_default=false()),
    Column("interest_halted", Boolean, nullable=False, server_default=false()),
    Column("deleted_at", UtcDateTime(), nullable=True),
    CheckConstraint("currency_id > 0", name="currency_id_positive"),
    CheckConstraint(f"hourly_rate_ppm BETWEEN 0 AND {RATE_PPM_MAX}", name="hourly_rate_range"),
    CheckConstraint(f"pending_rate_ppm IS NULL OR pending_rate_ppm BETWEEN 0 AND {RATE_PPM_MAX}", name="pending_rate_range"),
    CheckConstraint("(pending_rate_ppm IS NULL) = (rate_change_requested_at IS NULL)", name="pending_rate_pair"),
    CheckConstraint(
        f"REGEXP_LIKE(name, '{CURRENCY_NAME_PATTERN}', 'c') AND LOCATE('__', name) = 0 AND CHAR_LENGTH(name) - CHAR_LENGTH(REPLACE(name, '_', '')) <= {CURRENCY_NAME_UNDERSCORES_MAX}",
        name="name_format",
    ),
    CheckConstraint(f"REGEXP_LIKE(symbol, '{CURRENCY_SYMBOL_PATTERN}', 'c')", name="symbol_format"),
    ForeignKeyConstraint(["issuance_account_id", "currency_id"], ["account.account_id", "account.currency_id"], name=conv("fk_currency_issuance_account")),
    UniqueConstraint("issuance_account_id"),
    UniqueConstraint("active_symbol"),
    Index(None, "issuance_account_id", "currency_id"),
    Index(None, "created_by"),
    **TABLE_KWARGS,
)

balance = Table(
    "balance",
    metadata,
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("amount", Amount(), nullable=False),
    Column("allow_negative", Boolean, nullable=False),
    # Non-NULL only for rows allowed to go negative, and then it must name the currency whose issuance account account_id is.
    Column("negative_currency_id", BigInteger, Computed("(IF(allow_negative, currency_id, NULL))", persisted=True), nullable=True),
    PrimaryKeyConstraint("account_id", "currency_id"),
    CheckConstraint("amount >= 0 OR allow_negative", name="non_negative"),
    ForeignKeyConstraint(["account_id", "negative_currency_id"], ["account.account_id", "account.currency_id"], name=conv("fk_balance_negative_issuance")),
    Index(None, "currency_id"),
    Index(None, "account_id", "negative_currency_id"),
    **TABLE_KWARGS,
)

execution = Table(
    "execution",
    metadata,
    Column("execution_id", BigInteger, primary_key=True, autoincrement=True),
    Column("type", String(ENUM_LENGTH), nullable=False),
    Column("initiator_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("parent_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=True),
    Column("status", String(ENUM_LENGTH), nullable=False),
    Column("input", JSON, nullable=False),
    Column("output", JSON, nullable=True),
    Column("idempotency_key", String(IDEMPOTENCY_KEY_MAX), nullable=True),
    Column("contract_function", String(CONTRACT_FUNCTION_MAX), nullable=True),
    Column("created_at", UtcDateTime(), nullable=False),
    # The contract a contract_call called or a contract management execution acted on; it may name an account without a contract, so it has no foreign key.
    Column("contract_id", BigInteger, nullable=True),
    CheckConstraint(_in("status", ExecutionStatus), name="status"),
    UniqueConstraint("initiator_id", "idempotency_key"),
    Index(None, "initiator_id", "execution_id"),
    Index(None, "parent_execution_id"),
    Index(None, "type", "execution_id"),
    Index(None, "contract_function", "execution_id"),
    Index(None, "created_at"),
    Index(None, "contract_id", "execution_id"),
    **TABLE_KWARGS,
)

transfer = Table(
    "transfer",
    metadata,
    Column("transfer_id", BigInteger, primary_key=True, autoincrement=True),
    Column("execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("from_account", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("to_account", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("amount", Amount(), nullable=False),
    Column("kind", String(ENUM_LENGTH), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    CheckConstraint("from_account <> to_account", name="distinct_accounts"),
    CheckConstraint("amount > 0", name="amount_positive"),
    Index(None, "from_account", "transfer_id"),
    Index(None, "to_account", "transfer_id"),
    Index(None, "from_account", "created_at"),
    Index(None, "to_account", "created_at"),
    Index(None, "currency_id", "transfer_id"),
    Index(None, "execution_id"),
    Index(None, "created_at"),
    Index(None, "amount"),
    **TABLE_KWARGS,
)

allowance = Table(
    "allowance",
    metadata,
    Column("owner_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("spender_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("amount", Amount(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("owner_id", "spender_id", "currency_id"),
    CheckConstraint("amount >= 0", name="amount_non_negative"),
    CheckConstraint("owner_id <> spender_id", name="distinct_parties"),
    Index(None, "spender_id"),
    Index(None, "currency_id"),
    **TABLE_KWARGS,
)

allowance_history = Table(
    "allowance_history",
    metadata,
    Column("allowance_history_id", BigInteger, primary_key=True, autoincrement=True),
    Column("execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("owner_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("spender_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("old_amount", Amount(), nullable=False),
    Column("new_amount", Amount(), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    CheckConstraint("old_amount >= 0 AND new_amount >= 0", name="amounts_non_negative"),
    Index(None, "owner_id", "spender_id", "currency_id"),
    Index(None, "execution_id"),
    Index(None, "spender_id"),
    Index(None, "currency_id"),
    **TABLE_KWARGS,
)

claim = Table(
    "claim",
    metadata,
    Column("claim_id", BigInteger, primary_key=True, autoincrement=True),
    Column("claimant_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("payer_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("amount", Amount(), nullable=False),
    Column("description", String(CLAIM_DESCRIPTION_MAX), nullable=True),
    Column("status", String(ENUM_LENGTH), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Column("created_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("resolved_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=True),
    CheckConstraint("amount > 0", name="amount_positive"),
    CheckConstraint(_in("status", ClaimStatus), name="status"),
    CheckConstraint(f"(status = '{ClaimStatus.PENDING}') = (resolved_execution_id IS NULL)", name="resolution"),
    Index(None, "claimant_id", "claim_id"),
    Index(None, "payer_id", "claim_id"),
    Index(None, "currency_id", "status"),
    Index(None, "created_execution_id"),
    Index(None, "resolved_execution_id"),
    **TABLE_KWARGS,
)

stake = Table(
    "stake",
    metadata,
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("currency_id", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("amount", Amount(), nullable=False),
    Column("carry", BigInteger, nullable=False, server_default=text("0")),
    Column("last_accrued_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("account_id", "currency_id"),
    CheckConstraint("amount >= 0", name="amount_non_negative"),
    CheckConstraint(f"carry >= 0 AND carry < {CARRY_SCALE}", name="carry_range"),
    Index(None, "currency_id", "last_accrued_at"),
    **TABLE_KWARGS,
)

# A constant product pool identified by its ordered currency pair and fee tier; dissolution zeroes its reserves and shares and keeps the row as history, and "active" (1 while not dissolved, NULL after) lets at most one active pool exist per pair and tier.
# total_shares = locked_shares + the sum of pool_share.shares, where locked_shares is the MINIMUM_LIQUIDITY locked on the first deposit and owned by no provider.
pool = Table(
    "pool",
    metadata,
    Column("pool_id", BigInteger, primary_key=True, autoincrement=True),
    Column("currency_a", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("currency_b", BigInteger, ForeignKey("currency.currency_id"), nullable=False),
    Column("fee_bps", Integer, nullable=False),
    Column("reserve_a", Amount(), nullable=False),
    Column("reserve_b", Amount(), nullable=False),
    Column("total_shares", Amount(), nullable=False),
    Column("locked_shares", Amount(), nullable=False),
    Column("created_by", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("created_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("dissolved_at", UtcDateTime(), nullable=True),
    Column("dissolved_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=True),
    Column("active", Boolean, Computed("(CASE WHEN dissolved_at IS NULL THEN 1 END)", persisted=True), nullable=True),
    CheckConstraint("currency_a < currency_b", name="currency_order"),
    CheckConstraint(f"fee_bps IN ({', '.join(str(tier) for tier in FEE_TIERS_BPS)})", name="fee_tier"),
    CheckConstraint("reserve_a >= 0", name="reserve_a_non_negative"),
    CheckConstraint("reserve_b >= 0", name="reserve_b_non_negative"),
    CheckConstraint("total_shares >= 0", name="total_shares_non_negative"),
    CheckConstraint("locked_shares >= 0", name="locked_shares_non_negative"),
    CheckConstraint("(dissolved_at IS NULL) = (dissolved_execution_id IS NULL)", name="dissolution_pair"),
    CheckConstraint(
        f"(dissolved_at IS NULL AND reserve_a > 0 AND reserve_b > 0 AND locked_shares = {MINIMUM_LIQUIDITY} AND total_shares >= locked_shares)"
        " OR (dissolved_at IS NOT NULL AND reserve_a = 0 AND reserve_b = 0 AND total_shares = 0 AND locked_shares = 0)",
        name="state",
    ),
    UniqueConstraint("currency_a", "currency_b", "fee_bps", "active"),
    Index(None, "currency_b"),
    Index(None, "active", "pool_id"),
    Index(None, "created_by"),
    Index(None, "created_execution_id"),
    Index(None, "dissolved_execution_id"),
    **TABLE_KWARGS,
)

# A liquidity provider's shares in a pool; shares are not transferable, and a row is deleted when its shares reach zero.
pool_share = Table(
    "pool_share",
    metadata,
    Column("pool_id", BigInteger, ForeignKey("pool.pool_id"), nullable=False),
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("shares", Amount(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("pool_id", "account_id"),
    CheckConstraint("shares > 0", name="shares_positive"),
    Index(None, "account_id", "pool_id"),
    **TABLE_KWARGS,
)

# Every registered binary of a contract, kept as history: versions count up from 1 per account, and code holds the uncompressed bytes exactly as received.
contract_version = Table(
    "contract_version",
    metadata,
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("code", BLOB, nullable=False),
    Column("sha256", BINARY(32), nullable=False),
    Column("size", Integer, nullable=False),
    Column("created_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("account_id", "version"),
    CheckConstraint("account_id > 0", name="user_account"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(f"size BETWEEN 1 AND {CONTRACT_MAX_SIZE} AND size = LENGTH(code)", name="size"),
    CheckConstraint("sha256 = UNHEX(SHA2(code, 256))", name="sha256"),
    Index(None, "created_execution_id"),
    **TABLE_KWARGS,
)

# One contract per user account; deletion sets status deleted, clears the storage and the lock, and keeps the row and its versions, so registering again revives the row with the next version.
# current_version is the latest registered version, the one a non-deleted contract runs; locked_until forbids updates, pausing, resuming and deletion while it lies in the future, and only an active contract can be locked.
# storage_keys and storage_bytes count the contract_storage rows and their accounted sizes (UTF-8 bytes of the key plus value_size).
contract = Table(
    "contract",
    metadata,
    Column("account_id", BigInteger, ForeignKey("account.account_id"), primary_key=True, autoincrement=False),
    Column("status", String(ENUM_LENGTH), nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("locked_until", UtcDateTime(), nullable=True),
    Column("storage_keys", Integer, nullable=False, server_default=text("0")),
    Column("storage_bytes", Integer, nullable=False, server_default=text("0")),
    Column("created_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("updated_execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Column("deleted_at", UtcDateTime(), nullable=True),
    CheckConstraint("account_id > 0", name="user_account"),
    CheckConstraint(_in("status", ContractStatus), name="status"),
    CheckConstraint(f"(status = '{ContractStatus.DELETED}') = (deleted_at IS NOT NULL)", name="deletion_pair"),
    CheckConstraint(f"storage_keys BETWEEN 0 AND {CONTRACT_STORAGE_MAX_KEYS}", name="storage_keys_range"),
    CheckConstraint(f"storage_bytes BETWEEN 0 AND {CONTRACT_STORAGE_MAX_BYTES}", name="storage_bytes_range"),
    CheckConstraint(f"status <> '{ContractStatus.DELETED}' OR (storage_keys = 0 AND storage_bytes = 0 AND locked_until IS NULL)", name="deleted_state"),
    ForeignKeyConstraint(["account_id", "current_version"], ["contract_version.account_id", "contract_version.version"], name=conv("fk_contract_current_version")),
    Index(None, "account_id", "current_version"),
    Index(None, "created_execution_id"),
    Index(None, "updated_execution_id"),
    **TABLE_KWARGS,
)

# Public key-value storage of a contract; encoded_value holds the storage codec's bytes of one VM value.
contract_storage = Table(
    "contract_storage",
    metadata,
    Column("account_id", BigInteger, ForeignKey("contract.account_id"), nullable=False),
    Column("storage_key", String(CONTRACT_STORAGE_KEY_MAX), nullable=False),
    Column("encoded_value", BLOB, nullable=False),
    Column("value_size", Integer, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("account_id", "storage_key"),
    CheckConstraint("CHAR_LENGTH(storage_key) >= 1", name="key_not_empty"),
    CheckConstraint(f"value_size = LENGTH(encoded_value) AND value_size BETWEEN 1 AND {CONTRACT_STORAGE_VALUE_MAX}", name="value_size"),
    **TABLE_KWARGS,
)

# An external side effect queued by a committed transaction; attempts counts delivery attempts that reached or tried to reach Discord (rate-limit waits excluded), last_error describes the latest retryable failure, and a terminal row records its outcome in result (ints as decimal strings) and the time in completed_at.
outbox = Table(
    "outbox",
    metadata,
    Column("outbox_id", BigInteger, primary_key=True, autoincrement=True),
    Column("execution_id", BigInteger, ForeignKey("execution.execution_id"), nullable=True),
    Column("kind", String(ENUM_LENGTH), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(ENUM_LENGTH), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("available_at", UtcDateTime(), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("completed_at", UtcDateTime(), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("result", JSON, nullable=True),
    CheckConstraint(_in("status", OutboxStatus), name="status"),
    CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    CheckConstraint(f"(status = '{OutboxStatus.PENDING}') = (completed_at IS NULL)", name="completion"),
    CheckConstraint(f"(status = '{OutboxStatus.PENDING}') = (result IS NULL)", name="result"),
    Index(None, "status", "available_at"),
    Index(None, "execution_id"),
    **TABLE_KWARGS,
)

api_token = Table(
    "api_token",
    metadata,
    Column("api_token_id", BigInteger, primary_key=True, autoincrement=True),
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("token_hash", BINARY(32), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("revoked_at", UtcDateTime(), nullable=True),
    Column("last_used_at", UtcDateTime(), nullable=True),
    UniqueConstraint("token_hash"),
    Index(None, "account_id"),
    **TABLE_KWARGS,
)

# A row lets claims from allowed_user_id to user_id be sent to user_id as a direct message; without a row nothing is sent.
notification_permission = Table(
    "notification_permission",
    metadata,
    Column("user_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("allowed_user_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("user_id", "allowed_user_id"),
    CheckConstraint("user_id <> allowed_user_id", name="distinct_users"),
    Index(None, "allowed_user_id"),
    **TABLE_KWARGS,
)

# A row lets the contracts owned by account_id perform Discord operations in guild_id.
discord_permission = Table(
    "discord_permission",
    metadata,
    Column("guild_id", BigInteger, nullable=False),
    Column("account_id", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("granted_by", BigInteger, ForeignKey("account.account_id"), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("guild_id", "account_id"),
    Index(None, "account_id"),
    Index(None, "granted_by"),
    **TABLE_KWARGS,
)

job_lock = Table(
    "job_lock",
    metadata,
    Column("name", String(JOB_LOCK_NAME_MAX), primary_key=True),
    Column("holder", String(JOB_LOCK_HOLDER_MAX), nullable=False),
    Column("acquired_at", UtcDateTime(), nullable=False),
    Column("expires_at", UtcDateTime(), nullable=False),
    **TABLE_KWARGS,
)

# The guild directory tables below mirror the bot's gateway view of Discord for processes without a gateway; they are coordination state rewritten by the bot, not ledger data, and no write to them is an execution.
# The single row (state_id 1) records when the bot last confirmed its view was current; readers treat every directory row as unknown while the heartbeat is missing or stale.
guild_directory_state = Table(
    "guild_directory_state",
    metadata,
    Column("state_id", Integer, primary_key=True, autoincrement=False),
    Column("bot_user_id", BigInteger, nullable=True),
    Column("bot_heartbeat_at", UtcDateTime(), nullable=True),
    CheckConstraint(f"state_id = {GUILD_DIRECTORY_STATE_ID}", name="single_row"),
    CheckConstraint("bot_user_id IS NULL OR bot_user_id > 0", name="bot_user_id_positive"),
    **TABLE_KWARGS,
)

# A row means the bot is in the guild.
guild_snapshot = Table(
    "guild_snapshot",
    metadata,
    Column("guild_id", BigInteger, primary_key=True, autoincrement=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    CheckConstraint("guild_id > 0", name="guild_id_positive"),
    CheckConstraint("owner_id > 0", name="owner_id_positive"),
    **TABLE_KWARGS,
)

# The guild's roles that grant the Administrator permission; the @everyone role has the guild's own ID.
guild_administrator_role = Table(
    "guild_administrator_role",
    metadata,
    Column("guild_id", BigInteger, ForeignKey("guild_snapshot.guild_id", ondelete="CASCADE"), nullable=False),
    Column("role_id", BigInteger, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("guild_id", "role_id"),
    CheckConstraint("role_id > 0", name="role_id_positive"),
    **TABLE_KWARGS,
)

guild_member_snapshot = Table(
    "guild_member_snapshot",
    metadata,
    Column("guild_id", BigInteger, ForeignKey("guild_snapshot.guild_id", ondelete="CASCADE"), nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    PrimaryKeyConstraint("guild_id", "user_id"),
    CheckConstraint("user_id > 0", name="user_id_positive"),
    **TABLE_KWARGS,
)

# A member's roles other than @everyone, which every member has implicitly.
guild_member_role = Table(
    "guild_member_role",
    metadata,
    Column("guild_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("role_id", BigInteger, nullable=False),
    PrimaryKeyConstraint("guild_id", "user_id", "role_id"),
    ForeignKeyConstraint(["guild_id", "user_id"], ["guild_member_snapshot.guild_id", "guild_member_snapshot.user_id"], ondelete="CASCADE"),
    CheckConstraint("role_id > 0", name="role_id_positive"),
    Index(None, "guild_id", "role_id"),
    **TABLE_KWARGS,
)
