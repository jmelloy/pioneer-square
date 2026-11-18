"""Add S3 object references to api_request_log.

Foreman API payloads (system/messages/extra/response) move to private S3. This is the
expand step: new nullable reference columns are added and the inline JSON columns are kept
(legacy fallback during rollout). Existing rows are backfilled idempotently with
``python -m api_audit_store`` (PIONEER_AUDIT_BUCKET required); a later migration can drop the
inline columns once backfill is complete.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_000000"
down_revision = "20261116_000001"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("request_object_key", sa.Text()),
    ("request_object_bytes", sa.Integer()),
    ("request_sha256", sa.Text()),
    ("response_object_key", sa.Text()),
    ("response_object_bytes", sa.Integer()),
    ("response_sha256", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("api_request_log", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("api_request_log", name)
