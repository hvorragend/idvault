"""freigabe_pools: Pool-Zuweisung für Freigabe-Schritte

Revision ID: 0003_freigabe_pools
Revises: 0002_idv_draft
Create Date: 2026-04-22
"""

from __future__ import annotations

from alembic import op


revision = "0003_freigabe_pools"
down_revision = "0002_idv_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS freigabe_pools (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL UNIQUE,
            beschreibung TEXT,
            aktiv        INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS freigabe_pool_members (
            pool_id   INTEGER NOT NULL REFERENCES freigabe_pools(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES persons(id)        ON DELETE CASCADE,
            PRIMARY KEY (pool_id, person_id)
        )
    """)
    # pool_id-Spalte an idv_freigaben anfügen (alternative oder ergänzende Zuweisung)
    cols = {r[1] for r in bind.exec_driver_sql("PRAGMA table_info(idv_freigaben)").fetchall()}
    if "pool_id" not in cols:
        bind.exec_driver_sql(
            "ALTER TABLE idv_freigaben ADD COLUMN pool_id INTEGER REFERENCES freigabe_pools(id)"
        )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_freigaben_pool ON idv_freigaben(pool_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_freigaben_pool")
    # ALTER TABLE DROP COLUMN wurde erst in SQLite 3.35 eingeführt – best effort.
    try:
        bind.exec_driver_sql("ALTER TABLE idv_freigaben DROP COLUMN pool_id")
    except Exception:
        pass
    bind.exec_driver_sql("DROP TABLE IF EXISTS freigabe_pool_members")
    bind.exec_driver_sql("DROP TABLE IF EXISTS freigabe_pools")
