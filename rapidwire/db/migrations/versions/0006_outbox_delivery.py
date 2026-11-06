"""Outbox delivery records: the skipped status, completed_at for every terminal status (renamed from sent_at) and the JSON result of a terminal delivery.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_outbox_status"), "outbox", type_="check")
    op.alter_column("outbox", "sent_at", new_column_name="completed_at", existing_type=mysql.DATETIME(fsp=6), existing_nullable=True)
    op.add_column("outbox", sa.Column("result", sa.JSON(), nullable=True))
    # Terminal rows written before this revision get a completion time and a result naming their status.
    op.execute("UPDATE outbox SET completed_at = COALESCE(completed_at, created_at), result = JSON_OBJECT('status', status) WHERE status <> 'pending'")
    op.execute("UPDATE outbox SET completed_at = NULL WHERE status = 'pending'")
    op.create_check_constraint(op.f("ck_outbox_status"), "outbox", "status IN ('pending', 'sent', 'failed', 'skipped')")
    op.create_check_constraint(op.f("ck_outbox_completion"), "outbox", "(status = 'pending') = (completed_at IS NULL)")
    op.create_check_constraint(op.f("ck_outbox_result"), "outbox", "(status = 'pending') = (result IS NULL)")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_outbox_result"), "outbox", type_="check")
    op.drop_constraint(op.f("ck_outbox_completion"), "outbox", type_="check")
    op.drop_constraint(op.f("ck_outbox_status"), "outbox", type_="check")
    op.execute("UPDATE outbox SET status = 'failed' WHERE status = 'skipped'")
    op.drop_column("outbox", "result")
    op.alter_column("outbox", "completed_at", new_column_name="sent_at", existing_type=mysql.DATETIME(fsp=6), existing_nullable=True)
    op.create_check_constraint(op.f("ck_outbox_status"), "outbox", "status IN ('pending', 'sent', 'failed')")
