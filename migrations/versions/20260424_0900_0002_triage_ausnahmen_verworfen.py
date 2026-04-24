"""triage_ausnahmen_verworfen – Triage-Eintraege koennen verworfen werden

Revision ID: 0002_triage_ausnahmen_verworfen
Revises: 0001_initial_schema
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op

revision = "0002_triage_ausnahmen_verworfen"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS triage_ausnahmen_verworfen (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            kategorie        TEXT    NOT NULL,
            ref_key          TEXT    NOT NULL,
            verworfen_von_id INTEGER NOT NULL REFERENCES persons(id),
            verworfen_at     TEXT    NOT NULL DEFAULT (datetime('now','utc')),
            UNIQUE(kategorie, ref_key)
        )
    """)
    bind.exec_driver_sql("""
        CREATE INDEX IF NOT EXISTS idx_triage_ausnahmen_verworfen_kat
            ON triage_ausnahmen_verworfen(kategorie)
    """)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_ausnahmen_verworfen_kat")
    bind.exec_driver_sql("DROP TABLE IF EXISTS triage_ausnahmen_verworfen")
