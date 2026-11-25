"""PostgreSQL baseline (squash of the former MySQL revisions)

Revision ID: 9f1c2a7e5b30
Revises:
Create Date: 2026-11-25 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '9f1c2a7e5b30'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users: у реальній MySQL-базі був лише UNIQUE(user_id, group_id) — тому складений PK
    op.create_table(
        'users',
        sa.Column('user_id', sa.String(255), nullable=False),
        sa.Column('username', sa.String(255), nullable=True),
        sa.Column('group_id', sa.String(255), nullable=False),
        sa.Column('time', sa.DateTime(), nullable=True),
        sa.Column('first_name', sa.Text(), nullable=True),
        sa.Column('language_code', sa.String(100), nullable=True),
        sa.Column('title_gruop', sa.Text(), nullable=True),
        sa.Column('link_group', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('user_id', 'group_id'),
    )

    flags = ['creo', 'shop_google', 'shop_fb', 'console', 'apps', 'affiliate_mp', 'partner_mp', 'media_mt',
             'agency_google', 'agency_fb', 'media_mp', 'partner_mt']
    op.create_table(
        'chats',
        sa.Column('group_id', sa.String(255), nullable=False),
        sa.Column('title', sa.String(255), nullable=True),
        *[sa.Column(f, sa.Boolean(), server_default='0', nullable=False) for f in flags],
        sa.Column('time', sa.DateTime(), nullable=True),
        sa.Column('link', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('group_id'),
    )

    op.create_table(
        'admins',
        sa.Column('telegram_id', sa.String(255), nullable=False),
        sa.Column('name', sa.String(255), nullable=True),
        sa.Column('role', sa.String(255), server_default='ADMIN', nullable=True),
        sa.PrimaryKeyConstraint('telegram_id'),
    )

    op.create_table(
        'broadcasts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('admin_id', sa.String(255), nullable=False),
        sa.Column('admin_name', sa.String(255), nullable=True),
        sa.Column('category', sa.String(255), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=True),
        sa.Column('has_photo', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('has_buttons', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.Column('groups_count', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_broadcasts_sent_at', 'broadcasts', ['sent_at'])

    op.create_table(
        'chat_messages',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.String(255), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=True),
        sa.Column('reply_to_message_id', sa.BigInteger(), nullable=True),
        sa.Column('author_id', sa.String(255), nullable=True),
        sa.Column('author_username', sa.String(255), nullable=True),
        sa.Column('author_first_name', sa.String(255), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('message_type', sa.String(32), nullable=False),
        sa.Column('has_media', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.Column('edit_date', sa.DateTime(), nullable=True),
        sa.Column('is_edited', sa.Boolean(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'message_id', name='uq_chat_messages_group_msg'),
    )
    op.create_index('ix_chat_messages_group_sent', 'chat_messages', ['group_id', 'sent_at'])
    op.create_index('ix_chat_messages_sent', 'chat_messages', ['sent_at'])

    op.create_table(
        'mcp_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('token_prefix', sa.String(16), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('categories', sa.JSON(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('revoked', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_by', sa.String(255), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )

    op.create_table(
        'mcp_audit_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('key_id', sa.Integer(), nullable=True),
        sa.Column('method', sa.String(64), nullable=False),
        sa.Column('tool_name', sa.String(64), nullable=True),
        sa.Column('arguments', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('reason', sa.String(64), nullable=True),
        sa.Column('result_count', sa.Integer(), nullable=True),
        sa.Column('client_ip', sa.String(64), nullable=True),
        sa.Column('hits', sa.Integer(), server_default='1', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mcp_audit_key_created', 'mcp_audit_log', ['key_id', 'created_at'])
    op.create_index('ix_mcp_audit_created', 'mcp_audit_log', ['created_at'])


def downgrade() -> None:
    op.drop_table('mcp_audit_log')
    op.drop_table('mcp_keys')
    op.drop_table('chat_messages')
    op.drop_table('broadcasts')
    op.drop_table('admins')
    op.drop_table('chats')
    op.drop_table('users')
