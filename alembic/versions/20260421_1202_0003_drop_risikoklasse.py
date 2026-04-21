"""drop risikoklasse_id / risikoklassen

Revision ID: 0003_drop_risikoklasse
Revises: 0002_add_bearbeiter_name
Create Date: 2026-04-21

Ersetzt die ehemalige ``db._migrate_risikoklasse``-Funktion. Entfernt in
Legacy-Datenbanken die Spalte ``idv_register.risikoklasse_id`` und die
Tabelle ``risikoklassen``. Neu angelegte Datenbanken enthalten beide seit
Revision 0001 ohnehin nicht mehr – der Upgrade ist dann ein No-op.
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0003_drop_risikoklasse"
down_revision = "0002_add_bearbeiter_name"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    rows = op.get_bind().exec_driver_sql(
        f"PRAGMA table_info({table})"
    ).fetchall()
    return any(row[1] == column for row in rows)


def _has_table(table: str) -> bool:
    row = op.get_bind().exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def upgrade() -> None:
    # Reihenfolge: zuerst die FK-tragende Spalte entfernen, dann die
    # referenzierte Tabelle (wie in der ursprünglichen Python-Migration).
    if _has_column("idv_register", "risikoklasse_id"):
        with op.batch_alter_table("idv_register") as batch_op:
            batch_op.drop_column("risikoklasse_id")
    if _has_table("risikoklassen"):
        op.execute("DROP TABLE risikoklassen")


def downgrade() -> None:
    # Risikoklassen wurden fachlich komplett entfernt und werden nicht
    # wieder eingeführt – ein Downgrade hätte keine sinnvolle Semantik.
    raise NotImplementedError(
        "Downgrade von 0003_drop_risikoklasse wird nicht unterstützt."
    )
