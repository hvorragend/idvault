"""Tests für das Zurückstufen des Freigabe-Status beim Löschen eines
Verfahrensschritts (Issue: fix-release-status-update).

Hintergrund
-----------
Ein Admin kann einzelne Schritte des Test- & Freigabeverfahrens über
``webapp/routes/freigaben.py::loeschen`` entfernen. War das IDV bereits
``status='Freigegeben'`` bzw. ``teststatus='Freigegeben'``, blieb der
Freigabe-Status zuvor erhalten, obwohl ein tragender Verfahrensschritt
fehlt. Erwartetes Verhalten:

* Ohne explizite Bestätigung (``confirm_rollback=1``): Löschung wird nicht
  durchgeführt.
* Mit Bestätigung: Schritt wird gelöscht **und** ``status`` auf
  ``'In Prüfung'`` sowie ``teststatus`` auf ``'In Bearbeitung'``
  zurückgesetzt; zwei History-Einträge entstehen
  (``freigabe_schritt_geloescht`` und ``status_zurueckgesetzt``).

Die Tests arbeiten mit einer In-Memory-SQLite-Datenbank gegen das reale
``schema.sql`` und führen die Zustandsübergänge aus, die die Route
ausführt, wenn der ``released``-Pfad aktiviert ist.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

_SCHEMA_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "schema.sql"))


def _open_db() -> sqlite3.Connection:
    """In-Memory-DB mit Projekt-Schema (ohne Seed-Daten)."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    # FTS5 / externe Extensions überspringen, falls vorhanden – hier irrelevant.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def _insert_released_idv(conn: sqlite3.Connection) -> tuple[int, int]:
    """Legt ein freigegebenes IDV inkl. eines Freigabe-Schritts an.

    Returns (idv_db_id, freigabe_id).
    """
    cur = conn.execute(
        "INSERT INTO idv_register (idv_id, bezeichnung, status, teststatus) "
        "VALUES ('IDV-TST-001', 'Test-IDV', 'Freigegeben', 'Freigegeben')"
    )
    idv_db_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO idv_freigaben (idv_id, schritt, status) "
        "VALUES (?, 'Fachlicher Test', 'Erledigt')",
        (idv_db_id,),
    )
    return idv_db_id, cur.lastrowid


def _simulate_loeschen(conn: sqlite3.Connection, freigabe_id: int,
                       confirm_rollback: bool) -> bool:
    """Bildet die Zustandsübergänge von ``loeschen`` nach.

    Returns True, wenn ein Löschen stattgefunden hat, sonst False
    (entspricht dem Redirect-Fall ohne Bestätigung).
    """
    row = conn.execute(
        "SELECT idv_id, schritt FROM idv_freigaben WHERE id=?",
        (freigabe_id,),
    ).fetchone()
    assert row is not None, "Testvoraussetzung: Schritt existiert"
    idv_db_id = row["idv_id"]
    schritt = row["schritt"]
    idv = conn.execute(
        "SELECT status, teststatus FROM idv_register WHERE id=?",
        (idv_db_id,),
    ).fetchone()
    released = bool(
        idv and (idv["status"] == "Freigegeben"
                 or idv["teststatus"] == "Freigegeben")
    )
    if released and not confirm_rollback:
        return False

    conn.execute("DELETE FROM idv_freigaben WHERE id=?", (freigabe_id,))
    conn.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar) "
        "VALUES (?, 'freigabe_schritt_geloescht', ?)",
        (idv_db_id, f"{schritt} gelöscht"),
    )
    if released:
        conn.execute(
            "UPDATE idv_register SET status='In Prüfung', "
            "teststatus='In Bearbeitung' WHERE id=?",
            (idv_db_id,),
        )
        conn.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar) "
            "VALUES (?, 'status_zurueckgesetzt', ?)",
            (idv_db_id,
             f"Freigabe-Status zurückgesetzt (Schritt '{schritt}' gelöscht)"),
        )
    conn.commit()
    return True


class FreigabeRollbackTests(unittest.TestCase):

    # ── 1. Ohne Bestätigung bleibt alles unverändert ──────────────────
    def test_loeschen_ohne_confirm_bei_freigegebenem_idv_macht_nichts(self):
        conn = _open_db()
        idv_db_id, freigabe_id = _insert_released_idv(conn)

        did_delete = _simulate_loeschen(conn, freigabe_id, confirm_rollback=False)

        self.assertFalse(did_delete)
        # Schritt weiterhin vorhanden
        still_there = conn.execute(
            "SELECT COUNT(*) AS n FROM idv_freigaben WHERE id=?",
            (freigabe_id,),
        ).fetchone()["n"]
        self.assertEqual(still_there, 1)
        # Status unverändert
        idv = conn.execute(
            "SELECT status, teststatus FROM idv_register WHERE id=?",
            (idv_db_id,),
        ).fetchone()
        self.assertEqual(idv["status"], "Freigegeben")
        self.assertEqual(idv["teststatus"], "Freigegeben")
        # Keine History
        hist_count = conn.execute(
            "SELECT COUNT(*) AS n FROM idv_history WHERE idv_id=?",
            (idv_db_id,),
        ).fetchone()["n"]
        self.assertEqual(hist_count, 0)

    # ── 2. Mit Bestätigung: Status wird zurückgestuft ─────────────────
    def test_loeschen_mit_confirm_setzt_status_zurueck(self):
        conn = _open_db()
        idv_db_id, freigabe_id = _insert_released_idv(conn)

        did_delete = _simulate_loeschen(conn, freigabe_id, confirm_rollback=True)

        self.assertTrue(did_delete)
        # Schritt ist weg
        gone = conn.execute(
            "SELECT COUNT(*) AS n FROM idv_freigaben WHERE id=?",
            (freigabe_id,),
        ).fetchone()["n"]
        self.assertEqual(gone, 0)
        # Status heruntergestuft
        idv = conn.execute(
            "SELECT status, teststatus FROM idv_register WHERE id=?",
            (idv_db_id,),
        ).fetchone()
        self.assertEqual(idv["status"], "In Prüfung")
        self.assertEqual(idv["teststatus"], "In Bearbeitung")
        # Zwei History-Einträge: Löschung + Status-Reset
        aktionen = [
            r["aktion"] for r in conn.execute(
                "SELECT aktion FROM idv_history WHERE idv_id=? ORDER BY id",
                (idv_db_id,),
            )
        ]
        self.assertEqual(
            aktionen,
            ["freigabe_schritt_geloescht", "status_zurueckgesetzt"],
        )

    # ── 3. Nicht-freigegebenes IDV: alter Flow ohne Confirm ──────────
    def test_loeschen_bei_nicht_freigegebenem_idv_ohne_rollback(self):
        conn = _open_db()
        cur = conn.execute(
            "INSERT INTO idv_register (idv_id, bezeichnung, status, teststatus) "
            "VALUES ('IDV-TST-002', 'Test-IDV-2', 'In Prüfung', 'In Bearbeitung')"
        )
        idv_db_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO idv_freigaben (idv_id, schritt, status) "
            "VALUES (?, 'Technischer Test', 'Ausstehend')",
            (idv_db_id,),
        )
        freigabe_id = cur.lastrowid

        did_delete = _simulate_loeschen(conn, freigabe_id, confirm_rollback=False)

        self.assertTrue(did_delete)
        # Status bleibt (kein Rollback nötig)
        idv = conn.execute(
            "SELECT status, teststatus FROM idv_register WHERE id=?",
            (idv_db_id,),
        ).fetchone()
        self.assertEqual(idv["status"], "In Prüfung")
        self.assertEqual(idv["teststatus"], "In Bearbeitung")
        # Nur EIN History-Eintrag (Löschung), kein status_zurueckgesetzt
        aktionen = [
            r["aktion"] for r in conn.execute(
                "SELECT aktion FROM idv_history WHERE idv_id=?",
                (idv_db_id,),
            )
        ]
        self.assertEqual(aktionen, ["freigabe_schritt_geloescht"])


class TerminologieTests(unittest.TestCase):
    """Sicherstellen, dass das irreführende 'voller 3-Phasen-Workflow'-
    Wording nicht mehr in nutzerseitig sichtbaren Artefakten auftaucht."""

    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    _PATTERN = re.compile(r"(voller|Voller)\s+3-Phasen-Workflow", re.UNICODE)

    def _scan(self, relpath: str) -> list[tuple[int, str]]:
        with open(os.path.join(self._ROOT, relpath), "r", encoding="utf-8") as f:
            return [
                (i + 1, line.rstrip())
                for i, line in enumerate(f)
                if self._PATTERN.search(line)
            ]

    def test_sidebar_template_reworded(self):
        self.assertEqual(self._scan("webapp/templates/eigenentwicklung/_sidebar.html"), [])

    def test_freigaben_route_reworded(self):
        self.assertEqual(self._scan("webapp/routes/freigaben.py"), [])

    def test_anwendungsdokumentation_reworded(self):
        self.assertEqual(self._scan("docs/01-anwendungsdokumentation.md"), [])

    def test_aufsichtsrecht_reworded(self):
        self.assertEqual(self._scan("docs/07-aufsichtsrecht.md"), [])


if __name__ == "__main__":
    unittest.main()
