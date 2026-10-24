"""drop audiobook jobs

cplus no longer aligns books itself; alignments are uploaded finished. The job
table, and the alignment's link back to the job that made it, go. Finished
alignments are untouched.

Revision ID: b4d2e9a7c1f3
Revises: e2c8f4a61b37
Create Date: 2026-10-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b4d2e9a7c1f3'
down_revision: str | None = 'e2c8f4a61b37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('audiobook_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audiobook_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_audiobook_jobs_rating_key'))
    op.drop_table('audiobook_jobs')
    with op.batch_alter_table('audiobook_alignments', schema=None) as batch_op:
        batch_op.drop_column('job_id')


def downgrade() -> None:
    with op.batch_alter_table('audiobook_alignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('job_id', sa.Integer(), nullable=True))
    op.create_table('audiobook_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plex_server_id', sa.String(length=64), nullable=False),
        sa.Column('rating_key', sa.String(length=32), nullable=False),
        sa.Column('library_id', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('author', sa.String(length=512), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('stage', sa.String(length=128), nullable=True),
        sa.Column('progress', sa.Float(), server_default='0', nullable=False),
        sa.Column('eta_seconds', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('secret', sa.String(length=64), nullable=False),
        sa.Column('tracks', sa.JSON(), nullable=False),
        sa.Column('epub_title', sa.String(length=512), nullable=True),
        sa.Column('epub_author', sa.String(length=512), nullable=True),
        sa.Column('epub_words', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('heard_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audiobook_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audiobook_jobs_rating_key'), ['rating_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_audiobook_jobs_status'), ['status'], unique=False)
