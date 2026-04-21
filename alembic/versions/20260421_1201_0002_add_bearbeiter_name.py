"""add bearbeiter_name to idv_history

Revision ID: 0002_add_bearbeiter_name
Revises: 0001_initial_schema
Create Date: 2026-04-21

Ersetzt die ehemalige ``db._migrate_bearbeiter_name``-Funktion. Fügt die
Spalte ``bearbeiter_name`` in ``idv_history`` hinzu (Revisionssicherheit
für Config-User, deren Person-ID nicht in ``persons`` hinterlegt ist).

Idempotent: für Legacy-Datenbanken, die die Spalte schon via
``_migrate_bearbeiter_name`` erhalten haben, ist der Upgrade ein No-op.
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_add_bearbeiter_name"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    rows = op.get_bind().exec_driver_sql(
        f"PRAGMA table_info({table})"
    ).fetchall()
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    return any(row[1] == column for row in rows)


def upgrade() -> None:
    if not _has_column("idv_history", "bearbeiter_name"):
        op.execute("ALTER TABLE idv_history ADD COLUMN bearbeiter_name TEXT")


def downgrade() -> None:
    # SQLite kann Columns erst seit 3.35 droppen – Batch-Mode kapselt den
    # Table-Recreate. Wir nutzen das, damit Downgrades auch auf älteren
    # SQLite-Versionen funktionieren.
    with op.batch_alter_table("idv_history") as batch_op:
        batch_op.drop_column("bearbeiter_name")
