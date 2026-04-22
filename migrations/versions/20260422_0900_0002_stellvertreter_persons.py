"""P5: Stellvertreter-Felder in persons (Issue #216)

Fügt zwei Spalten zur persons-Tabelle hinzu:
  - stellvertreter_id  – allgemeiner persönlicher Stellvertreter (FK auf persons)
  - abwesend_bis       – ISO-Date, bis wann die Person abwesend ist

Wenn abwesend_bis >= CURRENT_DATE und stellvertreter_id gesetzt ist,
akzeptiert ensure_can_complete_schritt() den Stellvertreter als
berechtigt, Freigabe-Schritte abzuschließen.

Revision ID: 0002_stellvertreter_persons
Revises: 0001_initial_schema
Create Date: 2026-04-22
"""

from __future__ import annotations

from alembic import op

revision = "0002_stellvertreter_persons"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE persons ADD COLUMN stellvertreter_id INTEGER REFERENCES persons(id)"
    )
    bind.exec_driver_sql(
        "ALTER TABLE persons ADD COLUMN abwesend_bis TEXT"
    )


def downgrade() -> None:
    # SQLite unterstützt kein DROP COLUMN in älteren Versionen;
    # für idvault ist ein Downgrade hier nicht vorgesehen.
    raise NotImplementedError(
        "Downgrade dieser Revision wird nicht unterstützt."
    )
