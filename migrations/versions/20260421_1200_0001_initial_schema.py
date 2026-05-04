"""initial schema (schema.sql)

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-21

Erste Alembic-Revision: spielt den in ``schema.sql`` definierten
Zielzustand ein. Die Statements werden einzeln über
``exec_driver_sql`` ausgeführt, damit Alembic die Migration und das
Schreiben in ``alembic_version`` in einer gemeinsamen SA-Transaktion
halten kann – ``sqlite3.Connection.executescript`` würde zwischendurch
implizit committen und den Transaktionsstatus zerreißen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

from alembic import op


# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _schema_sql_path() -> Path:
    """Liefert den Pfad zu schema.sql – auch im PyInstaller-Bundle.

    Sidecar-Overlay hat Vorrang: liegt eine ``schema.sql`` im
    ``updates/``-Verzeichnis neben der EXE bzw. neben ``run.py``,
    wird sie statt der gebundelten Version verwendet. So kann eine
    neue Schema-Definition ohne EXE-Rebuild ausgerollt werden.
    """
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2])
    overlay = base / "updates" / "schema.sql"
    if overlay.is_file():
        return overlay
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "schema.sql"
    # alembic/versions/<datei>.py → zwei Ebenen hoch zur Projektwurzel
    return Path(__file__).resolve().parents[2] / "schema.sql"


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Zerlegt eine SQL-Skript-Datei in Einzelstatements.

    Respektiert ``'``-Stringliterale (inkl. ``''``-Escape) und
    ``-- …``-Zeilenkommentare – schema.sql enthält u. a. Texte mit
    Semikolons (``'5 per minute;30 per hour'``) und innerhalb von Strings
    den Comment-Marker ``--``. Ein naives ``.split(';')`` würde hier
    Statements zerreißen.
    """
    buf: list[str] = []
    in_string = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if not in_string and ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # Zeilenkommentar bis Zeilenende überspringen.
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "'":
            # Verdoppeltes Hochkomma innerhalb eines Strings ist Escape.
            if in_string and i + 1 < n and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_string = not in_string
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_string:
            stmt = "".join(buf).strip()
            if stmt:
                yield stmt
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def upgrade() -> None:
    schema_path = _schema_sql_path()
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql nicht gefunden: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")

    bind = op.get_bind()
    for stmt in _iter_sql_statements(sql):
        # PRAGMA-Statements werden von alembic bereits in env.py gesetzt
        # und tauchen in schema.sql nur informativ auf – überspringen, um
        # innerhalb der aktiven Transaktion keine PRAGMA-Nebenwirkungen
        # anzustoßen (journal_mode z. B. ist in Transaktionen ein No-op).
        if stmt.upper().startswith("PRAGMA"):
            continue
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    # Downgrade der Ur-Revision entspräche einem Drop aller Tabellen – das
    # ist unwiederbringlich und für idvscope nicht vorgesehen.
    raise NotImplementedError(
        "Downgrade der Initial-Revision wird nicht unterstützt."
    )
