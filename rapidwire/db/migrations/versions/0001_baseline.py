"""Baseline: pin the database default character set and binary collation so later tables do not depend on the server configuration.

Revision ID: 0001
Revises:
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Without a database name, ALTER DATABASE applies to the connection's default database, which also works in offline --sql mode.
    op.execute("ALTER DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")


def downgrade() -> None:
    pass
