"""alert outbox and delivery attempts

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13 01:00:00

Phase 3 (hand-written; autogenerate cannot see renames or CHECK changes):
  * threshold_configs: reminder_interval_minutes -> cooldown_minutes (data kept), plus
    eligible_sources, monitoring_interval_minutes, breach_episode, last_evaluation_*.
  * alert_events: outbox columns (rendered message, retry schedule, lease) and the new
    delivery status "sending" / event type "test".
  * alert_delivery_attempts: one row per delivery attempt.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_OLD = "delivery_status IN ('pending', 'sent', 'failed', 'suppressed')"
DELIVERY_NEW = "delivery_status IN ('pending', 'sending', 'sent', 'failed', 'suppressed')"
EVENT_OLD = "event_type IN ('breached', 'reminder', 'recovered')"
EVENT_NEW = "event_type IN ('breached', 'reminder', 'recovered', 'test')"
EVAL_STATUS = (
    "last_evaluation_status IN ('ok', 'breached', 'no_data', 'stale', 'currency_mismatch', "
    "'incompatible_units', 'disabled')"
)


def upgrade() -> None:
    with op.batch_alter_table('threshold_configs', schema=None) as batch_op:
        batch_op.drop_constraint('reminder_interval_min', type_='check')
        batch_op.alter_column('reminder_interval_minutes', new_column_name='cooldown_minutes',
                              existing_type=sa.Integer(), existing_nullable=True)
        batch_op.add_column(sa.Column('eligible_sources', sa.Text(), nullable=False,
                                      server_default='["api_verified", "manual"]'))
        batch_op.add_column(sa.Column('monitoring_interval_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('breach_episode', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('last_evaluation_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('last_evaluation_message', sa.String(length=255), nullable=True))
        batch_op.create_check_constraint(
            'cooldown_min', 'cooldown_minutes IS NULL OR cooldown_minutes >= 15')
        batch_op.create_check_constraint(
            'monitoring_interval_min',
            'monitoring_interval_minutes IS NULL OR monitoring_interval_minutes >= 5')
        batch_op.create_check_constraint(
            'episode_non_negative', 'breach_episode >= 0')
        batch_op.create_check_constraint(
            'last_evaluation_status', EVAL_STATUS)

    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_constraint('delivery_status', type_='check')
        batch_op.drop_constraint('event_type', type_='check')
        batch_op.create_check_constraint('delivery_status', DELIVERY_NEW)
        batch_op.create_check_constraint('event_type', EVENT_NEW)
        batch_op.add_column(sa.Column('episode', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('subject', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('body_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('body_html', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('context', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('next_attempt_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_alert_events_due', ['delivery_status', 'next_attempt_at'],
                              unique=False)

    op.create_table(
        'alert_delivery_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('alert_event_id', sa.Integer(), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(length=32), nullable=False),
        sa.Column('error_code', sa.String(length=32), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('smtp_status', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('attempt_number >= 1', name=op.f('ck_alert_delivery_attempts_attempt_positive')),
        sa.CheckConstraint("outcome IN ('sent', 'retry', 'failed')",
                           name=op.f('ck_alert_delivery_attempts_outcome')),
        sa.ForeignKeyConstraint(['alert_event_id'], ['alert_events.id'],
                                name=op.f('fk_alert_delivery_attempts_alert_event_id_alert_events'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_delivery_attempts')),
        sa.UniqueConstraint('alert_event_id', 'attempt_number',
                            name=op.f('uq_alert_delivery_attempts_alert_event_id_attempt_number')),
    )
    with op.batch_alter_table('alert_delivery_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_delivery_attempts_alert_event_id'),
                              ['alert_event_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('alert_delivery_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_delivery_attempts_alert_event_id'))
    op.drop_table('alert_delivery_attempts')

    # Rows using values that the old constraints don't allow cannot be kept.
    op.execute("DELETE FROM alert_events WHERE event_type = 'test'")
    op.execute("UPDATE alert_events SET delivery_status = 'pending' WHERE delivery_status = 'sending'")
    with op.batch_alter_table('alert_events', schema=None) as batch_op:
        batch_op.drop_index('ix_alert_events_due')
        for column in ('lease_expires_at', 'next_attempt_at', 'context', 'body_html', 'body_text',
                       'subject', 'episode'):
            batch_op.drop_column(column)
        batch_op.drop_constraint('event_type', type_='check')
        batch_op.drop_constraint('delivery_status', type_='check')
        batch_op.create_check_constraint('event_type', EVENT_OLD)
        batch_op.create_check_constraint('delivery_status', DELIVERY_OLD)

    with op.batch_alter_table('threshold_configs', schema=None) as batch_op:
        for name in ('last_evaluation_status', 'episode_non_negative', 'monitoring_interval_min',
                     'cooldown_min'):
            batch_op.drop_constraint(name, type_='check')
        for column in ('last_evaluation_message', 'last_evaluation_status', 'breach_episode',
                       'monitoring_interval_minutes', 'eligible_sources'):
            batch_op.drop_column(column)
        batch_op.alter_column('cooldown_minutes', new_column_name='reminder_interval_minutes',
                              existing_type=sa.Integer(), existing_nullable=True)
        batch_op.create_check_constraint(
            'reminder_interval_min',
            'reminder_interval_minutes IS NULL OR reminder_interval_minutes >= 15')
