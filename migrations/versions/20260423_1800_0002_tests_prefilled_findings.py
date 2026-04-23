"""tests_prefilled_findings (Pruefzeugnis technische Abnahme, Issue #349)

Revision ID: 0002_tests_prefilled_findings
Revises: 0001_initial_schema
Create Date: 2026-04-23

Fuehrt die Tabelle ``tests_prefilled_findings`` ein. Sie persistiert
ausschliesslich Abweichungen vom maschinellen Scanner-Befund (Makros,
externe Verknuepfungen, Blatt-/Zellschutz, Formelanzahl, SHA-256,
Dateigroesse/Sheets). Eine fehlende Zeile bedeutet "maschinell
bestaetigt, ungeaendert" – eine Zeile mit ``manual_override=1`` haelt
die manuelle Korrektur samt Pflichtkommentar und Prueferin/Pruefer
fest.
"""

from __future__ import annotations

from alembic import op


revision = "0002_tests_prefilled_findings"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS tests_prefilled_findings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id              INTEGER NOT NULL REFERENCES technischer_test(id) ON DELETE CASCADE,
            file_id              INTEGER NOT NULL REFERENCES idv_files(id)        ON DELETE CASCADE,
            check_kind           TEXT    NOT NULL,
            machine_result       TEXT,
            source_scan_run_id   INTEGER,
            manual_override      INTEGER NOT NULL DEFAULT 0,
            manual_comment       TEXT,
            confirmed_by_id      INTEGER REFERENCES persons(id),
            recorded_at          TEXT NOT NULL DEFAULT (datetime('now','utc')),
            UNIQUE (test_id, file_id, check_kind)
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tests_prefilled_test "
        "ON tests_prefilled_findings(test_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tests_prefilled_file "
        "ON tests_prefilled_findings(file_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tests_prefilled_file")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tests_prefilled_test")
    bind.exec_driver_sql("DROP TABLE IF EXISTS tests_prefilled_findings")
