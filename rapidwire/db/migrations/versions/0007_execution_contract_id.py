"""The contract of an execution: execution.contract_id names the contract a contract_call called or a contract management execution acted on, indexed with execution_id, and existing rows are filled in.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A contract_call takes input.contract_id when it is a decimal of 1 to 19 digits without a leading zero and at most the BIGINT maximum.
# The text is given an explicit collation so the pattern match does not depend on the connection's collation, and the CASE converts only matching text, so strict mode never sees a conversion warning.
_BACKFILL_CALLS = (
    "UPDATE execution SET contract_id = (CASE"
    " WHEN REGEXP_LIKE(JSON_UNQUOTE(JSON_EXTRACT(input, '$.contract_id')) COLLATE utf8mb4_bin, '^[1-9][0-9]{0,18}$')"
    " THEN (CASE WHEN CAST(JSON_UNQUOTE(JSON_EXTRACT(input, '$.contract_id')) AS DECIMAL(19, 0)) <= 9223372036854775807"
    " THEN CAST(JSON_UNQUOTE(JSON_EXTRACT(input, '$.contract_id')) AS SIGNED) END) END)"
    " WHERE type = 'contract_call'"
)


def upgrade() -> None:
    op.add_column("execution", sa.Column("contract_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_execution_contract_id_execution_id"), "execution", ["contract_id", "execution_id"], unique=False)
    op.execute("UPDATE execution SET contract_id = initiator_id WHERE type IN ('contract_update', 'contract_lock', 'contract_pause', 'contract_resume', 'contract_delete')")
    op.execute(_BACKFILL_CALLS)


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_contract_id_execution_id"), table_name="execution")
    op.drop_column("execution", "contract_id")
