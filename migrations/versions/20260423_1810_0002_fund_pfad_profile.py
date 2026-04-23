"""fund_pfad_profile

Revision ID: 0002_fund_pfad_profile
Revises: 0001_initial_schema
Create Date: 2026-04-23

Legt die Tabelle ``fund_pfad_profile`` an, die von der Admin-Ansicht
``/admin/pfad-profile`` und vom Bulk-Registrierungs-Flow
(``webapp/routes/eigenentwicklung.py::_best_fund_pfad_profil``) genutzt
wird. Vor dieser Revision fehlte die Tabelle in Bestands-DBs, was zu
``sqlite3.OperationalError: no such table: fund_pfad_profile`` führte.
"""

from __future__ import annotations

from alembic import op


revision = "0002_fund_pfad_profile"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS fund_pfad_profile (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            pfad_praefix             TEXT    NOT NULL UNIQUE,
            org_unit_id              INTEGER REFERENCES org_units(id),
            fachverantwortlicher_id  INTEGER REFERENCES persons(id),
            idv_koordinator_id       INTEGER REFERENCES persons(id),
            entwicklungsart          TEXT,
            pruefintervall_monate    INTEGER,
            bemerkung                TEXT,
            aktiv                    INTEGER NOT NULL DEFAULT 1,
            created_at               TEXT    NOT NULL DEFAULT (datetime('now','utc')),
            created_by_id            INTEGER REFERENCES persons(id),
            updated_at               TEXT
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_fund_pfad_profile_aktiv "
        "ON fund_pfad_profile(aktiv)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_fund_pfad_profile_aktiv")
    bind.exec_driver_sql("DROP TABLE IF EXISTS fund_pfad_profile")
