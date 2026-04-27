"""triage_ausnahmen_verworfen → triage_verworfen umbenennen

Revision ID: 0003_rename_triage_verworfen
Revises: 0002_triage_ausnahmen_verworfen
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op


revision = "0003_rename_triage_verworfen"
down_revision = "0002_triage_ausnahmen_verworfen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Index zuerst loswerden, damit der Name spaeter wieder vergeben werden
    # kann, ohne dass SQLite ueber Doppelvergabe stolpert.
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_ausnahmen_verworfen_kat")
    bind.exec_driver_sql(
        "ALTER TABLE triage_ausnahmen_verworfen RENAME TO triage_verworfen"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_triage_verworfen_kat "
        "ON triage_verworfen(kategorie)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_verworfen_kat")
    bind.exec_driver_sql(
        "ALTER TABLE triage_verworfen RENAME TO triage_ausnahmen_verworfen"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_triage_ausnahmen_verworfen_kat "
        "ON triage_ausnahmen_verworfen(kategorie)"
    )
