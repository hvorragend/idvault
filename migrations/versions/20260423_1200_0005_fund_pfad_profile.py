"""fund_pfad_profile: Pfad-Profile für Default-Kopfdaten bei Bulk-Registrierung

Revision ID: 0005_fund_pfad_profile
Revises: 0004_archiviert_sichtbar
Create Date: 2026-04-23
"""

from __future__ import annotations

from alembic import op


revision = "0005_fund_pfad_profile"
down_revision = "0004_archiviert_sichtbar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS fund_pfad_profile (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            pfad_praefix            TEXT    NOT NULL UNIQUE,
            org_unit_id             INTEGER REFERENCES org_units(id),
            fachverantwortlicher_id INTEGER REFERENCES persons(id),
            idv_koordinator_id      INTEGER REFERENCES persons(id),
            entwicklungsart         TEXT,
            pruefintervall_monate   INTEGER,
            bemerkung               TEXT,
            aktiv                   INTEGER NOT NULL DEFAULT 1,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now','utc')),
            created_by_id           INTEGER REFERENCES persons(id),
            updated_at              TEXT
        )
    """)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_fund_pfad_profile_aktiv "
        "ON fund_pfad_profile(aktiv)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_fund_pfad_profile_aktiv")
    bind.exec_driver_sql("DROP TABLE IF EXISTS fund_pfad_profile")
