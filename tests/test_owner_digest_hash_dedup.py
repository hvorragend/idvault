"""Tests für die Hash-Dedup-Logik beim Versand von Owner-Sammelmails
(Issue: skip-email-duplicate-file).

Hintergrund
-----------
``webapp/notification_scheduler.py::_dispatch_owner_digest`` selektiert
die offenen Funde (``idv_files`` mit ``bearbeitungsstatus='Neu'``), um
sie pro File-Owner gebündelt per Mail zu versenden. Bisher wurde nur
geprüft, ob die *konkrete Datei* (``f.id``) bereits in ``idv_register``
oder ``idv_file_links`` hängt. Eine inhaltlich identische Kopie unter
einem anderen Pfad (gleicher ``file_hash``) wurde dadurch erneut
gemeldet.

Die Tests fahren das echte Projekt-Schema in einer In-Memory-SQLite hoch
und führen das SELECT der Funktion 1:1 aus. Geprüft wird, dass:

* eine Datei, deren Hash bereits an einem IDV hängt
  (``idv_register.file_id``), aus dem Ergebnis fliegt;
* dasselbe gilt, wenn die Verknüpfung über ``idv_file_links`` erfolgt;
* eine Datei mit unbekanntem Hash weiterhin als offener Fund erscheint.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

_SCHEMA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "schema.sql")
)


def _open_db() -> sqlite3.Connection:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def _extract_owner_digest_select(source_path: str) -> str:
    """Liest das SELECT aus ``_dispatch_owner_digest`` aus dem Quelltext.

    So bleibt der Test an die Produktiv-Query gekoppelt: jede Änderung
    am SQL fließt automatisch in den Test ein, ohne dass die Query hier
    dupliziert werden müsste.
    """
    with open(source_path, "r", encoding="utf-8") as f:
        src = f.read()
    # Erstes triple-quoted-SQL nach der Funktionsdefinition reicht – die
    # Funktion enthält genau ein größeres SELECT.
    m = re.search(
        r"def _dispatch_owner_digest\b.*?db\.execute\(\"\"\"(.*?)\"\"\"\)",
        src,
        re.DOTALL,
    )
    assert m, "SELECT in _dispatch_owner_digest nicht gefunden"
    return m.group(1)


def _insert_person(conn: sqlite3.Connection, user_id: str, email: str) -> int:
    cur = conn.execute(
        "INSERT INTO persons (user_id, nachname, vorname, email, aktiv) "
        "VALUES (?, 'Mustermann', 'Max', ?, 1)",
        (user_id, email),
    )
    return cur.lastrowid


def _insert_file(
    conn: sqlite3.Connection,
    *,
    full_path: str,
    file_hash: str,
    owner: str,
    status_neu: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO idv_files
            (file_hash, full_path, file_name, extension,
             file_owner, status, bearbeitungsstatus)
        VALUES (?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            file_hash,
            full_path,
            os.path.basename(full_path),
            os.path.splitext(full_path)[1].lstrip("."),
            owner,
            "Neu" if status_neu else "Registriert",
        ),
    )
    return cur.lastrowid


def _insert_idv(conn: sqlite3.Connection, idv_id: str, file_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO idv_register (idv_id, bezeichnung, file_id) VALUES (?, ?, ?)",
        (idv_id, f"IDV {idv_id}", file_id),
    )
    return cur.lastrowid


class OwnerDigestHashDedupTests(unittest.TestCase):
    """Verifiziert die Hash-Dedup-Filter im Owner-Digest-SELECT."""

    @classmethod
    def setUpClass(cls) -> None:
        scheduler_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                os.pardir,
                "webapp",
                "notification_scheduler.py",
            )
        )
        cls.select_sql = _extract_owner_digest_select(scheduler_path)

    def setUp(self) -> None:
        self.db = _open_db()
        _insert_person(self.db, "muster.max", "max@example.org")

    def tearDown(self) -> None:
        self.db.close()

    def _query_open_finds(self) -> list[sqlite3.Row]:
        return self.db.execute(self.select_sql).fetchall()

    # ------------------------------------------------------------------
    # Positiv-Kontrolle
    # ------------------------------------------------------------------
    def test_neue_datei_ohne_registrierung_wird_gemeldet(self) -> None:
        _insert_file(
            self.db,
            full_path=r"\\share\dir\datei.xlsx",
            file_hash="HASH-A",
            owner="muster.max",
        )
        self.db.commit()
        rows = self._query_open_finds()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["full_path"], r"\\share\dir\datei.xlsx")

    # ------------------------------------------------------------------
    # Hash-Dedup: identische Kopie mit anderem Pfad
    # ------------------------------------------------------------------
    def test_identische_datei_via_idv_register_wird_unterdrueckt(self) -> None:
        # Erste Kopie ist bereits an einem IDV registriert
        registered_id = _insert_file(
            self.db,
            full_path=r"\\share\dir\original.xlsx",
            file_hash="HASH-A",
            owner="muster.max",
            status_neu=False,
        )
        _insert_idv(self.db, "IDV-2026-001", registered_id)
        # Neue Kopie unter anderem Pfad, gleicher Hash
        _insert_file(
            self.db,
            full_path=r"\\share\dir\kopie.xlsx",
            file_hash="HASH-A",
            owner="muster.max",
        )
        self.db.commit()

        rows = self._query_open_finds()
        self.assertEqual(
            rows, [],
            "Inhaltlich identische Datei darf nicht erneut gemeldet werden, "
            "wenn die Originalkopie bereits via idv_register hängt.",
        )

    def test_identische_datei_via_file_links_wird_unterdrueckt(self) -> None:
        # IDV ohne primären file_id, Verknüpfung via idv_file_links
        registered_id = _insert_file(
            self.db,
            full_path=r"\\share\dir\original.xlsx",
            file_hash="HASH-B",
            owner="muster.max",
            status_neu=False,
        )
        idv_db_id = _insert_idv(self.db, "IDV-2026-002", None)
        self.db.execute(
            "INSERT INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
            (idv_db_id, registered_id),
        )
        # Neue Kopie unter anderem Pfad, gleicher Hash
        _insert_file(
            self.db,
            full_path=r"\\share\dir\kopie.xlsx",
            file_hash="HASH-B",
            owner="muster.max",
        )
        self.db.commit()

        rows = self._query_open_finds()
        self.assertEqual(
            rows, [],
            "Hash-Dedup muss auch greifen, wenn die Originalkopie nur "
            "über idv_file_links an einem IDV hängt.",
        )

    def test_unbekannter_hash_bleibt_offener_fund(self) -> None:
        # Eine Datei mit anderem Hash ist registriert ...
        registered_id = _insert_file(
            self.db,
            full_path=r"\\share\dir\original.xlsx",
            file_hash="HASH-OTHER",
            owner="muster.max",
            status_neu=False,
        )
        _insert_idv(self.db, "IDV-2026-003", registered_id)
        # ... und ein neuer Fund mit eigenständigem Hash darf nicht
        # versehentlich mit weggefiltert werden.
        new_id = _insert_file(
            self.db,
            full_path=r"\\share\dir\neu.xlsx",
            file_hash="HASH-NEW",
            owner="muster.max",
        )
        self.db.commit()

        rows = self._query_open_finds()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], new_id)


if __name__ == "__main__":
    unittest.main()
