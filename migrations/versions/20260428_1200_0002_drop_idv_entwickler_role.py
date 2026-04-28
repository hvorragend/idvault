"""Drop IDV-Entwickler role

Revision ID: 0002_drop_idv_entwickler_role
Revises: 0001_initial_schema
Create Date: 2026-04-28

Hintergrund: Die Session-Rolle ``IDV-Entwickler`` wurde abgeschafft. Wer
als ``idv_entwickler_id`` an einer IDV eingetragen ist, behält die
Beteiligten-Rechte über den Ownership-Check; eine separate Anmelde-
Rolle ist nicht mehr nötig. Diese Migration räumt bestehende
Zuordnungen auf, damit der Wert nicht mehr im UI/Filter auftaucht.
"""

from __future__ import annotations

from alembic import op


revision = "0002_drop_idv_entwickler_role"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Bestehende Personen mit dieser Rolle: Rolle leeren. Person bleibt
    # aktiv, bleibt an ihren IDVs als Entwickler beteiligt und kann sich
    # weiterhin anmelden — sie hat lediglich keine Anmelde-Rolle mehr.
    bind.exec_driver_sql(
        "UPDATE persons SET rolle = NULL WHERE rolle = 'IDV-Entwickler'"
    )
    # AD-Gruppen-Mappings, die diese Rolle vergeben, entfernen.
    bind.exec_driver_sql(
        "DELETE FROM ldap_group_role_mapping WHERE rolle = 'IDV-Entwickler'"
    )


def downgrade() -> None:
    # Es gibt keine verlässliche Rückabbildung: welche Personen vorher
    # die Rolle hatten, lässt sich aus den verbleibenden Daten nicht mehr
    # rekonstruieren.
    raise NotImplementedError(
        "Downgrade der IDV-Entwickler-Bereinigung wird nicht unterstützt."
    )
