"""payment reconciliation

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14 10:00:00

Phase 4: a payment changes a balance only through an explicit owner reconciliation,
which is recorded on the payment (timestamp + the manual balance snapshot it created).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('payment_transactions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reconciled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('reconciled_balance_snapshot_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_payment_transactions_reconciled_balance_snapshot_id_provider_balance_snapshots'),
            'provider_balance_snapshots', ['reconciled_balance_snapshot_id'], ['id'],
            ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('payment_transactions', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('fk_payment_transactions_reconciled_balance_snapshot_id_provider_balance_snapshots'),
            type_='foreignkey')
        batch_op.drop_column('reconciled_balance_snapshot_id')
        batch_op.drop_column('reconciled_at')
