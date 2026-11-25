"""OAuth 2.1 для MCP: mcp_oauth_clients, mcp_oauth_tokens

Revision ID: c4e8a1d7b296
Revises: 3b7d9e4c1a52
Create Date: 2026-11-25 22:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4e8a1d7b296'
down_revision = '3b7d9e4c1a52'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_oauth_clients',
        sa.Column('client_id', sa.String(64), primary_key=True),
        sa.Column('client_name', sa.String(255), nullable=True),
        sa.Column('redirect_uris', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('client_ip', sa.String(64), nullable=True),
    )
    op.create_table(
        'mcp_oauth_tokens',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('key_id', sa.Integer(), sa.ForeignKey('mcp_keys.id'), nullable=False),
        sa.Column('client_id', sa.String(64), nullable=False),
        sa.Column('family_id', sa.String(32), nullable=False),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_mcp_oauth_tokens_family', 'mcp_oauth_tokens', ['family_id'])
    op.create_index('ix_mcp_oauth_tokens_key', 'mcp_oauth_tokens', ['key_id'])
    op.create_index('ix_mcp_oauth_tokens_expires', 'mcp_oauth_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_mcp_oauth_tokens_expires', table_name='mcp_oauth_tokens')
    op.drop_index('ix_mcp_oauth_tokens_key', table_name='mcp_oauth_tokens')
    op.drop_index('ix_mcp_oauth_tokens_family', table_name='mcp_oauth_tokens')
    op.drop_table('mcp_oauth_tokens')
    op.drop_table('mcp_oauth_clients')
