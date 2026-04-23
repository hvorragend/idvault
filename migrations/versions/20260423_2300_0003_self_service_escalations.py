"""Self-Service-Eskalationen (Issue #355)

Revision ID: 0003_self_service_escalations
Revises: 0003_silent_release
Create Date: 2026-04-23

Erweitert ``persons`` um ``oe_leiter_id`` (optionaler Vorgesetzter / OE-
Leiter, an den die zweite Eskalations-Stufe gemailt wird) und legt die
Default-Settings fuer das dreistufige Eskalations-Verfahren an:
``escalation_reminder_days`` (Stufe 1, Default 7),
``escalation_to_lead_days`` (Stufe 2, Default 14),
``escalation_to_coordinator_days`` (Stufe 3, Default 21).

Anmerkung: Ursprünglich gegen ``0002`` aufgesetzt, weil parallel zu den
anderen ``0003_*``-Revisionen entwickelt. Nach dem Merge der vorherigen
PRs in ``main`` wurde die Kette linearisiert:
``0001 → 0002 → 0003_testfall_vorlage_scope → 0003_silent_release →
0003_self_service_escalations``.
"""

from __future__ import annotations

from alembic import op


revision = "0003_self_service_escalations"
down_revision = "0003_silent_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = bind.exec_driver_sql("PRAGMA table_info(persons)").fetchall()
    if not any(c[1] == "oe_leiter_id" for c in cols):
        bind.exec_driver_sql(
            "ALTER TABLE persons "
            "ADD COLUMN oe_leiter_id INTEGER REFERENCES persons(id)"
        )
    bind.exec_driver_sql(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES "
        "('escalation_reminder_days', '7')"
    )
    bind.exec_driver_sql(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES "
        "('escalation_to_lead_days', '14')"
    )
    bind.exec_driver_sql(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES "
        "('escalation_to_coordinator_days', '21')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "DELETE FROM app_settings WHERE key IN ("
        "'escalation_reminder_days','escalation_to_lead_days',"
        "'escalation_to_coordinator_days')"
    )
    # SQLite kann Spalten nur via Tabelle-Rebuild droppen; Spalte bleibt erhalten.
