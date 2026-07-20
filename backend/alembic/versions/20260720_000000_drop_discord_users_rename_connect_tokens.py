"""Drop discord_users; rename discord_connect_tokens -> discord_pending_connects.

``discord_users`` (GitHub login -> Discord ID, hand-populated via the removed
``/api/discord-users`` endpoints) predated the self-serve ``/connect-account``
flow and duplicated what ``discord_account_links`` now records authoritatively.
Its one remaining reader, ``discord_notifier.mention_or_login``, resolves
through ``users.github_login`` -> ``discord_account_links`` instead.

Legacy rows are backfilled into ``discord_account_links`` where the GitHub
login matches a known user and the Discord account is not already linked, so
existing mentions keep working; rows for unknown logins have no Pioneer Square
user to attach to and are dropped with the table.

``discord_connect_tokens`` is renamed to ``discord_pending_connects`` to make
it obvious the table holds short-lived, single-use handshake state rather than
a durable mapping like its neighbours.

Revision ID: 20260720_000000_drop_discord_users
Revises: 20260715_000000_add_bot_fields_to_users
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_000000_drop_discord_users"
down_revision: str | Sequence[str] | None = "20260715_000000_add_bot_fields_to_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Backfill before dropping. users.github_login is stored as GitHub returns
    # it while discord_users.github_login was lowercased on write, so match on
    # lower(). ON CONFLICT guards the discord_user_id unique index: an account
    # already linked via /connect-account keeps that (authoritative) link.
    op.execute(
        sa.text(
            """
            INSERT INTO discord_account_links
                (ps_user_id, discord_user_id, discord_username, created_at)
            SELECT u.id, du.discord_user_id, '', du.created_at
            FROM discord_users du
            JOIN users u ON lower(u.github_login) = du.github_login
            ON CONFLICT (discord_user_id) DO NOTHING
            """
        )
    )
    op.drop_table("discord_users")

    # Postgres keeps the old index/constraint names through a table rename;
    # rename them too so the schema doesn't still say "connect_tokens".
    op.rename_table("discord_connect_tokens", "discord_pending_connects")
    op.execute(
        sa.text(
            "ALTER INDEX discord_connect_tokens_pkey RENAME TO discord_pending_connects_pkey"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE discord_pending_connects "
            "RENAME CONSTRAINT discord_connect_tokens_token_key "
            "TO discord_pending_connects_token_key"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE discord_pending_connects "
            "RENAME CONSTRAINT discord_pending_connects_token_key "
            "TO discord_connect_tokens_token_key"
        )
    )
    op.execute(
        sa.text(
            "ALTER INDEX discord_pending_connects_pkey RENAME TO discord_connect_tokens_pkey"
        )
    )
    op.rename_table("discord_pending_connects", "discord_connect_tokens")
    op.create_table(
        "discord_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("github_login", sa.String(), nullable=False, unique=True),
        sa.Column("discord_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Rows backfilled by upgrade() are indistinguishable from ones created
    # directly by /connect-account, so they are not moved back — the table is
    # recreated empty and mention_or_login falls back to plain @login text.
