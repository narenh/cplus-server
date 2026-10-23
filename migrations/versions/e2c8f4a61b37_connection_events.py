"""connection events

How each tvOS client reaches Plex: one row per install and one per decision
it reports. New tables only.

Revision ID: e2c8f4a61b37
Revises: a7c3e5f19b28
Create Date: 2026-10-22 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e2c8f4a61b37'
down_revision: str | None = 'a7c3e5f19b28'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('client_devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('client_identifier', sa.String(length=128), nullable=False),
    sa.Column('device_name', sa.String(length=256), nullable=True),
    sa.Column('profile', sa.String(length=64), server_default='', nullable=False),
    sa.Column('app_version', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('current_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_uri', sa.String(length=512), nullable=True),
    sa.Column('current_tier', sa.String(length=16), nullable=True),
    sa.Column('current_network', sa.String(length=16), nullable=True),
    sa.Column('last_lan_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_local_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'client_identifier')
    )
    op.create_index(
        op.f('ix_client_devices_user_id'), 'client_devices', ['user_id'], unique=False
    )
    op.create_table('connection_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('profile', sa.String(length=64), server_default='', nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('network', sa.String(length=16), nullable=True),
    sa.Column('app_version', sa.String(length=32), nullable=True),
    sa.Column('server_machine_id', sa.String(length=64), nullable=True),
    sa.Column('previous_uri', sa.String(length=512), nullable=True),
    sa.Column('previous_tier', sa.String(length=16), nullable=True),
    sa.Column('chosen_uri', sa.String(length=512), nullable=True),
    sa.Column('chosen_tier', sa.String(length=16), nullable=True),
    sa.Column('chosen_via_443', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('candidates', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['client_devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        op.f('ix_connection_events_received_at'),
        'connection_events',
        ['received_at'],
        unique=False,
    )
    op.create_index(
        'ix_connection_events_device_at', 'connection_events', ['device_id', 'at'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_connection_events_device_at', table_name='connection_events')
    op.drop_index(op.f('ix_connection_events_received_at'), table_name='connection_events')
    op.drop_table('connection_events')
    op.drop_index(op.f('ix_client_devices_user_id'), table_name='client_devices')
    op.drop_table('client_devices')
