"""triage_ausnahmen_verworfen → triage_verworfen umbenennen

Revision ID: 0003_rename_triage_verworfen
Revises: 0002_triage_ausnahmen_verworfen
Create Date: 2026-04-27

Defensiv geschrieben, weil ``schema.sql`` (von 0001 eingespielt) bereits
auf den neuen Namen umgestellt ist:
- frische DB: 0001 -> triage_verworfen, 0002 -> zusaetzlich
  triage_ausnahmen_verworfen, 0003 muss nun beide zusammenfuehren.
- bestehende DB an 0002: nur triage_ausnahmen_verworfen vorhanden,
  klassischer Rename.
"""

from __future__ import annotations

from alembic import op


revision = "0003_rename_triage_verworfen"
down_revision = "0002_triage_ausnahmen_verworfen"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    row = bind.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_ausnahmen_verworfen_kat")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_verworfen_kat")

    has_old = _table_exists(bind, "triage_ausnahmen_verworfen")
    has_new = _table_exists(bind, "triage_verworfen")

    if has_old and not has_new:
        # Klassischer Rename auf einer DB, die noch auf 0002 stand.
        bind.exec_driver_sql(
            "ALTER TABLE triage_ausnahmen_verworfen RENAME TO triage_verworfen"
        )
    elif has_old and has_new:
        # Frische DB: 0001 hat triage_verworfen via schema.sql angelegt,
        # 0002 zusaetzlich triage_ausnahmen_verworfen. Daten der alten
        # Tabelle defensiv uebernehmen, dann alte Tabelle droppen.
        bind.exec_driver_sql(
            "INSERT OR IGNORE INTO triage_verworfen "
            "(kategorie, ref_key, verworfen_von_id, verworfen_at) "
            "SELECT kategorie, ref_key, verworfen_von_id, verworfen_at "
            "  FROM triage_ausnahmen_verworfen"
        )
        bind.exec_driver_sql("DROP TABLE triage_ausnahmen_verworfen")
    elif not has_old and not has_new:
        # Weder neu noch alt - sollte praktisch nicht passieren, aber
        # sicherheitshalber die Zieltabelle erzeugen.
        bind.exec_driver_sql("""
            CREATE TABLE triage_verworfen (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                kategorie        TEXT    NOT NULL,
                ref_key          TEXT    NOT NULL,
                verworfen_von_id INTEGER REFERENCES persons(id),
                verworfen_at     TEXT    NOT NULL DEFAULT (datetime('now','utc')),
                UNIQUE(kategorie, ref_key)
            )
        """)
    # has_new only: nichts zu tun.

    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_triage_verworfen_kat "
        "ON triage_verworfen(kategorie)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_triage_verworfen_kat")
    if _table_exists(bind, "triage_verworfen"):
        bind.exec_driver_sql(
            "ALTER TABLE triage_verworfen RENAME TO triage_ausnahmen_verworfen"
        )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_triage_ausnahmen_verworfen_kat "
        "ON triage_ausnahmen_verworfen(kategorie)"
    )
