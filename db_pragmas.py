"""
Zentrale PRAGMA-Konfiguration für alle SQLite-Verbindungen.

Einheitliche Werte zwischen Webapp, Scanner-Subprozess und Writer-Thread
verhindern, dass sich Akteure gegenseitig mit unterschiedlichen
busy_timeouts aussperren (siehe Commit 65662d9).
"""

from __future__ import annotations

import sqlite3
from typing import Literal

Role = Literal["writer", "reader"]


def apply_pragmas(conn: sqlite3.Connection, *, role: Role = "reader") -> None:
    """Setzt die einheitlichen SQLite-PRAGMAs.

    Reader und Writer teilen sich dieselbe WAL-Konfiguration; nur
    `wal_autocheckpoint` greift für den Writer (Checkpoints werden beim
    Committen des Writers angestoßen).
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -20000")  # ~20 MB

    if role == "writer":
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
