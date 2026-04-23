"""Rename legacy status 'Genehmigt' → 'Freigegeben'

Revision ID: 0002_rename_genehmigt_to_freigegeben
Revises: 0001_initial_schema
Create Date: 2026-04-23

Benennt den veralteten Status-Wert 'Genehmigt' in 'Freigegeben' um
(bzw. 'Genehmigt mit Auflagen' → 'Freigegeben mit Auflagen').
Betroffen: idv_register.status und idv_register.teststatus.
"""

from __future__ import annotations

from alembic import op


revision = "0002_rename_genehmigt_to_freigegeben"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE idv_register
        SET status = 'Freigegeben'
        WHERE status = 'Genehmigt'
    """)
    op.execute("""
        UPDATE idv_register
        SET status = 'Freigegeben mit Auflagen'
        WHERE status = 'Genehmigt mit Auflagen'
    """)
    op.execute("""
        UPDATE idv_register
        SET teststatus = 'Freigegeben'
        WHERE teststatus = 'Genehmigt'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE idv_register
        SET status = 'Genehmigt'
        WHERE status = 'Freigegeben'
    """)
    op.execute("""
        UPDATE idv_register
        SET status = 'Genehmigt mit Auflagen'
        WHERE status = 'Freigegeben mit Auflagen'
    """)
    op.execute("""
        UPDATE idv_register
        SET teststatus = 'Genehmigt'
        WHERE teststatus = 'Freigegeben'
    """)
