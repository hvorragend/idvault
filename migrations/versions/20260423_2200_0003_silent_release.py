"""silent_release Spalte + Setting (Issue #351)

Revision ID: 0003_silent_release
Revises: 0003_testfall_vorlage_scope
Create Date: 2026-04-23

Fuehrt die ``Stille Freigabe`` als verkuerztes Verfahren fuer
nicht-wesentliche Eigenentwicklungen ein. Erweitert das Statusmodell
um die Variante ``Freigegeben (Stille Freigabe)`` und persistiert das
verwendete Verfahren in einer neuen Spalte ``freigabe_verfahren`` von
``idv_register``. Die App-Setting ``silent_release_enabled`` ist als
Opt-In hinterlegt (Default: aus).

Anmerkung: Ursprünglich gegen ``0002`` aufgesetzt, weil parallel zu
``0003_testfall_vorlage_scope`` (Issue #350) entwickelt. Nach dem
Merge beider PRs in ``main`` wurde diese Revision auf
``0003_testfall_vorlage_scope`` umkettet, damit ``alembic upgrade head``
eindeutig bleibt.
"""

from __future__ import annotations

from alembic import op


revision = "0003_silent_release"
down_revision = "0003_testfall_vorlage_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = bind.exec_driver_sql("PRAGMA table_info(idv_register)").fetchall()
    if not any(c[1] == "freigabe_verfahren" for c in cols):
        bind.exec_driver_sql(
            "ALTER TABLE idv_register "
            "ADD COLUMN freigabe_verfahren TEXT NOT NULL DEFAULT 'Standard'"
        )
    bind.exec_driver_sql(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES "
        "('silent_release_enabled', '0')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    # SQLite kann keine Spalten droppen ohne Tabelle neu zu bauen — Spalte
    # bleibt erhalten, hat aber Default 'Standard'. Setting entfernen.
    bind.exec_driver_sql(
        "DELETE FROM app_settings WHERE key='silent_release_enabled'"
    )
