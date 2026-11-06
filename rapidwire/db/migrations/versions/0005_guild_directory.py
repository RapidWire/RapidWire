"""Guild directory: the bot's heartbeat and its snapshot of guilds, administrator roles, members and member roles.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_KWARGS: dict[str, Any] = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin", "mysql_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "guild_directory_state",
        sa.Column("state_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("bot_user_id", sa.BigInteger(), nullable=True),
        sa.Column("bot_heartbeat_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint("state_id = 1", name=op.f("ck_guild_directory_state_single_row")),
        sa.CheckConstraint("bot_user_id IS NULL OR bot_user_id > 0", name=op.f("ck_guild_directory_state_bot_user_id_positive")),
        sa.PrimaryKeyConstraint("state_id", name=op.f("pk_guild_directory_state")),
        **_TABLE_KWARGS,
    )
    op.create_table(
        "guild_snapshot",
        sa.Column("guild_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("guild_id > 0", name=op.f("ck_guild_snapshot_guild_id_positive")),
        sa.CheckConstraint("owner_id > 0", name=op.f("ck_guild_snapshot_owner_id_positive")),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_guild_snapshot")),
        **_TABLE_KWARGS,
    )
    op.create_table(
        "guild_administrator_role",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("role_id > 0", name=op.f("ck_guild_administrator_role_role_id_positive")),
        sa.ForeignKeyConstraint(["guild_id"], ["guild_snapshot.guild_id"], name=op.f("fk_guild_administrator_role_guild_id_guild_snapshot"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "role_id", name=op.f("pk_guild_administrator_role")),
        **_TABLE_KWARGS,
    )
    op.create_table(
        "guild_member_snapshot",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint("user_id > 0", name=op.f("ck_guild_member_snapshot_user_id_positive")),
        sa.ForeignKeyConstraint(["guild_id"], ["guild_snapshot.guild_id"], name=op.f("fk_guild_member_snapshot_guild_id_guild_snapshot"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "user_id", name=op.f("pk_guild_member_snapshot")),
        **_TABLE_KWARGS,
    )
    op.create_table(
        "guild_member_role",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("role_id > 0", name=op.f("ck_guild_member_role_role_id_positive")),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_member_snapshot.guild_id", "guild_member_snapshot.user_id"],
            name=op.f("fk_guild_member_role_guild_id_guild_member_snapshot"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("guild_id", "user_id", "role_id", name=op.f("pk_guild_member_role")),
        **_TABLE_KWARGS,
    )
    op.create_index(op.f("ix_guild_member_role_guild_id_role_id"), "guild_member_role", ["guild_id", "role_id"], unique=False)


def downgrade() -> None:
    op.drop_table("guild_member_role")
    op.drop_table("guild_member_snapshot")
    op.drop_table("guild_administrator_role")
    op.drop_table("guild_snapshot")
    op.drop_table("guild_directory_state")
