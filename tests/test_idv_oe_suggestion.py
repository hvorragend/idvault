"""Tests fuer den OE-Vorschlag bei der IDV-Registrierung.

Hintergrund
-----------
Bei der Anlage einer neuen Eigenentwicklung wird die zustaendige
Organisationseinheit (OE) automatisch vorausgefuellt. Bisher kam dafuer
ausschliesslich die OE des eingeloggten Benutzers in Betracht (U-C3).
Hat der ausgewaehlte Entwickler in den Stammdaten bereits eine OE
hinterlegt, soll diese bevorzugt vorgeschlagen werden.

Geprueft wird:

* Die Personensuche (``/admin/api/persons/search``) liefert die
  ``org_unit_id`` jedes Treffers mit, damit das Frontend die OE
  vorschlagen kann.
* Die Server-seitige Vorbelegung in ``new_idv`` priorisiert die
  Entwickler-OE gegenueber der OE des eingeloggten Benutzers, fuellt
  aber auf letztere zurueck, sobald kein Entwickler bekannt ist.
"""

from __future__ import annotations

import os
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
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_persons(conn: sqlite3.Connection) -> dict:
    """Legt zwei OEs und drei Personen an. Rueckgabe: Lookup-Dict."""
    cur = conn.execute(
        "INSERT INTO org_units (bezeichnung) VALUES ('Risikocontrolling')"
    )
    ou_risk = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO org_units (bezeichnung) VALUES ('Treasury')"
    )
    ou_treasury = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO persons (user_id, nachname, vorname, email, aktiv, org_unit_id) "
        "VALUES ('reg', 'Regis', 'Rita', NULL, 1, ?)",
        (ou_risk,),
    )
    p_registrar = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO persons (user_id, nachname, vorname, email, aktiv, org_unit_id) "
        "VALUES ('dev', 'Devin', 'Dora', NULL, 1, ?)",
        (ou_treasury,),
    )
    p_developer = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO persons (user_id, nachname, vorname, email, aktiv, org_unit_id) "
        "VALUES ('xy', 'Solo', 'Sam', NULL, 1, NULL)"
    )
    p_no_ou = cur.lastrowid

    return {
        "ou_risk": ou_risk,
        "ou_treasury": ou_treasury,
        "p_registrar": p_registrar,
        "p_developer": p_developer,
        "p_no_ou": p_no_ou,
    }


# ---- 1:1-Replik der SELECT-Klausel aus stammdaten.api_persons_search ----
_SEARCH_SQL = """
    SELECT id, user_id, nachname, vorname, email, aktiv, org_unit_id
      FROM persons
     WHERE user_id LIKE ?
        OR nachname LIKE ?
        OR vorname  LIKE ?
        OR email    LIKE ?
     ORDER BY aktiv DESC,
              (CASE WHEN user_id LIKE ? THEN 0 ELSE 1 END),
              nachname COLLATE NOCASE,
              vorname  COLLATE NOCASE
     LIMIT ?
"""


def _resolve_prefill_ou(conn: sqlite3.Connection,
                        prefill: dict,
                        registrar_pid: int | None) -> dict:
    """Bildet die OE-Vorbelegungslogik aus ``new_idv`` nach.

    Die Funktion bekommt das (sonst von Scanner-Fund / Draft befuellte)
    ``prefill``-Dict und das ``person_id`` des eingeloggten Benutzers
    und schreibt ``prefill["org_unit_id"]`` analog zum Server-Verhalten.
    """
    if not prefill.get("org_unit_id"):
        dev_pid = prefill.get("idv_entwickler_id")
        if dev_pid:
            row = conn.execute(
                "SELECT org_unit_id FROM persons WHERE id = ?", (dev_pid,)
            ).fetchone()
            if row and row["org_unit_id"]:
                prefill["org_unit_id"] = row["org_unit_id"]
    if not prefill.get("org_unit_id") and registrar_pid:
        row = conn.execute(
            "SELECT org_unit_id FROM persons WHERE id = ?", (registrar_pid,)
        ).fetchone()
        if row and row["org_unit_id"]:
            prefill["org_unit_id"] = row["org_unit_id"]
    return prefill


class PersonSearchOuTests(unittest.TestCase):
    """Sicherstellung, dass die Personensuche die OE-Zuordnung exportiert."""

    def setUp(self) -> None:
        self.conn = _open_db()
        self.ids = _seed_persons(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_search_response_contains_org_unit_id(self) -> None:
        rows = self.conn.execute(
            _SEARCH_SQL,
            ("%dev%", "%dev%", "%dev%", "%dev%", "dev%", 15),
        ).fetchall()
        match = next((r for r in rows if r["user_id"] == "dev"), None)
        self.assertIsNotNone(match, "Entwicklerin 'dev' muss gefunden werden")
        self.assertEqual(match["org_unit_id"], self.ids["ou_treasury"])

    def test_search_response_includes_persons_without_ou(self) -> None:
        rows = self.conn.execute(
            _SEARCH_SQL,
            ("%xy%", "%xy%", "%xy%", "%xy%", "xy%", 15),
        ).fetchall()
        match = next((r for r in rows if r["user_id"] == "xy"), None)
        self.assertIsNotNone(match)
        self.assertIsNone(match["org_unit_id"])


class PrefillOuPriorityTests(unittest.TestCase):
    """OE-Vorbelegung in ``new_idv`` (Server-seitige Reihenfolge)."""

    def setUp(self) -> None:
        self.conn = _open_db()
        self.ids = _seed_persons(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_developer_ou_wins_over_registrar(self) -> None:
        prefill = {"idv_entwickler_id": self.ids["p_developer"]}
        result = _resolve_prefill_ou(
            self.conn, prefill, registrar_pid=self.ids["p_registrar"]
        )
        self.assertEqual(result["org_unit_id"], self.ids["ou_treasury"])

    def test_registrar_ou_used_when_no_developer(self) -> None:
        prefill: dict = {}
        result = _resolve_prefill_ou(
            self.conn, prefill, registrar_pid=self.ids["p_registrar"]
        )
        self.assertEqual(result["org_unit_id"], self.ids["ou_risk"])

    def test_falls_back_to_registrar_when_developer_has_no_ou(self) -> None:
        prefill = {"idv_entwickler_id": self.ids["p_no_ou"]}
        result = _resolve_prefill_ou(
            self.conn, prefill, registrar_pid=self.ids["p_registrar"]
        )
        self.assertEqual(result["org_unit_id"], self.ids["ou_risk"])

    def test_existing_prefill_ou_is_kept(self) -> None:
        prefill = {
            "idv_entwickler_id": self.ids["p_developer"],
            "org_unit_id": self.ids["ou_risk"],
        }
        result = _resolve_prefill_ou(
            self.conn, prefill, registrar_pid=self.ids["p_registrar"]
        )
        self.assertEqual(result["org_unit_id"], self.ids["ou_risk"])

    def test_no_ou_when_neither_developer_nor_registrar_has_one(self) -> None:
        prefill = {"idv_entwickler_id": self.ids["p_no_ou"]}
        result = _resolve_prefill_ou(
            self.conn, prefill, registrar_pid=self.ids["p_no_ou"]
        )
        self.assertNotIn("org_unit_id", result)


if __name__ == "__main__":
    unittest.main()
