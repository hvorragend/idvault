"""freigabe_claim: bearbeitet_von_id-Spalte für Pool-Claim (#321)

Revision ID: 0007_freigabe_claim
Revises: 0006_testfall_vorlagen
Create Date: 2026-04-23
"""

from __future__ import annotations

from alembic import op


revision = "0007_freigabe_claim"
down_revision = "0006_testfall_vorlagen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {r[1] for r in bind.exec_driver_sql(
        "PRAGMA table_info(idv_freigaben)"
    ).fetchall()}
    if "bearbeitet_von_id" not in cols:
        bind.exec_driver_sql(
            "ALTER TABLE idv_freigaben "
            "ADD COLUMN bearbeitet_von_id INTEGER REFERENCES persons(id)"
        )
    if "bearbeitet_am" not in cols:
        bind.exec_driver_sql(
            "ALTER TABLE idv_freigaben ADD COLUMN bearbeitet_am TEXT"
        )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_freigaben_bearbeitet_von "
        "ON idv_freigaben(bearbeitet_von_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_freigaben_bearbeitet_von")
    try:
        bind.exec_driver_sql(
            "ALTER TABLE idv_freigaben DROP COLUMN bearbeitet_von_id"
        )
    except Exception:
        pass
    try:
        bind.exec_driver_sql(
            "ALTER TABLE idv_freigaben DROP COLUMN bearbeitet_am"
        )
    except Exception:
        pass
