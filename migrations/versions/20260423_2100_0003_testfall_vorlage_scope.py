"""testfall_vorlage_scope (Auto-Auswahl von Vorlagen, Issue #350)

Revision ID: 0003_testfall_vorlage_scope
Revises: 0002_tests_prefilled_findings
Create Date: 2026-04-23

Fuehrt die Tabelle ``testfall_vorlage_scope`` ein. Eine Vorlage kann
ueber 0..n Scope-Eintraege auf bestimmte Organisationseinheiten und/oder
Klassifikationen (wesentlich / nicht wesentlich) eingeschraenkt werden.
Eintraege mit ``oe_id IS NULL`` gelten fuer alle OEs, mit
``klassifikation IS NULL`` fuer alle Klassifikationen. Das Flag
``mandatory`` markiert einen Scope als verpflichtend; mindestens ein
passender Scope mit ``mandatory=1`` macht die Vorlage bei der
Pruefungs-Anlage zur Pflicht.
"""

from __future__ import annotations

from alembic import op


revision = "0003_testfall_vorlage_scope"
down_revision = "0002_tests_prefilled_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS testfall_vorlage_scope (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vorlage_id      INTEGER NOT NULL REFERENCES testfall_vorlagen(id) ON DELETE CASCADE,
            oe_id           INTEGER REFERENCES org_units(id) ON DELETE CASCADE,
            klassifikation  TEXT CHECK (klassifikation IN ('wesentlich','nicht wesentlich')),
            mandatory       INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','utc'))
        )
        """
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tv_scope_vorlage "
        "ON testfall_vorlage_scope(vorlage_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tv_scope_oe "
        "ON testfall_vorlage_scope(oe_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tv_scope_oe")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tv_scope_vorlage")
    bind.exec_driver_sql("DROP TABLE IF EXISTS testfall_vorlage_scope")
