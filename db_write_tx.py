"""
write_tx – atomarer Schreibtransaktions-Kontextmanager.

Multi-Statement-Writes (z. B. `create_idv`: INSERT + History + UPDATE)
werden in `BEGIN IMMEDIATE` eingeschlossen. Damit erwirbt SQLite die
Writer-Sperre *sofort* bei Transaktionsstart und nicht mitten im ersten
UPDATE — das verhindert SQLITE_BUSY-Eskalationen, wenn parallel der
Scanner oder ein anderer Writer schreibt.

Bei Lock-Kollisionen wird bis zu 3× mit exponentiellem Backoff erneut
versucht; der busy_timeout der Connection (60 s, siehe db_pragmas)
fängt die meisten Fälle bereits innerhalb von BEGIN IMMEDIATE ab.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager


_MAX_RETRIES = 3
_BASE_BACKOFF_S = 0.1  # 100 ms, 200 ms, 400 ms


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Öffnet BEGIN IMMEDIATE → yield → COMMIT (ROLLBACK bei Exception).

    Nutzung:
        with write_tx(conn):
            conn.execute(...)
            conn.execute(...)
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            # Falls eine implizite Transaktion noch offen ist (in_transaction),
            # zuerst sauber abschließen.
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                raise
            time.sleep(_BASE_BACKOFF_S * (2 ** attempt))
    else:  # pragma: no cover — break oben verlässt die Schleife
        if last_exc is not None:
            raise last_exc

    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    else:
        conn.commit()
