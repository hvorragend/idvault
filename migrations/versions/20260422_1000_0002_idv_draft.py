"""idv_draft: Draft-Persistenz für Eigenentwicklungs-Formular

Revision ID: 0002_idv_draft
Revises: 0001_initial_schema
Create Date: 2026-04-22
"""

from __future__ import annotations

from alembic import op


revision = "0002_idv_draft"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS idv_draft (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            draft_json  TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            UNIQUE(user_id)
        )
    """)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS idv_draft")
