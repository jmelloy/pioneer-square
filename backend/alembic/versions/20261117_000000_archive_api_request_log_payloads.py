"""Add S3 object references to api_request_log (issue #1487).

Additive only (N-1 safe): the inline JSON columns stay and are cleared by the app once a
payload is durably archived. Existing rows are backfilled by the app sweeper, idempotently.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261117_000000"
down_revision = "20261116_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_request_log", sa.Column("request_object_key", sa.String(), nullable=True))
    op.add_column("api_request_log", sa.Column("response_object_key", sa.String(), nullable=True))
    op.add_column(
        "api_request_log",
        sa.Column("payload_archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "api_request_log",
        sa.Column("payload_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_request_log", "payload_expires_at")
    op.drop_column("api_request_log", "payload_archived_at")
    op.drop_column("api_request_log", "response_object_key")
    op.drop_column("api_request_log", "request_object_key")
