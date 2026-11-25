"""mcp_keys.deleted_at (м'яке видалення ключів MCP)

Revision ID: 3b7d9e4c1a52
Revises: 9f1c2a7e5b30
Create Date: 2026-11-25 20:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '3b7d9e4c1a52'
down_revision = '9f1c2a7e5b30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('mcp_keys', sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('mcp_keys', 'deleted_at')
