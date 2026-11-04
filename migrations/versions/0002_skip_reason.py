"""raw_documents.skip_reason: why a document was skipped (tech-phase0.md section 3.1, week-1 review item 10).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ADD_SKIP_REASON = """
ALTER TABLE raw_documents ADD COLUMN skip_reason text;   -- skipped일 때 too_short|lang_undetected|…, 그 외 NULL
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(ADD_SKIP_REASON)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE raw_documents DROP COLUMN skip_reason")
