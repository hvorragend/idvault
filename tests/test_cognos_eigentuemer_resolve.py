"""Tests für ``_resolve_person_by_eigentuemer`` und die
Bewertungsanforderungs-E-Mail für Cognos-Berichte.

Hintergrund (Issues #458 und #459)
----------------------------------
Cognos exportiert die Spalte „Eigentümer" in unterschiedlichen
Schreibweisen (``DOMAIN\\login``, ``Lastname, Firstname (login)``,
``login (Vorname Nachname)`` …). Der bisherige Lookup deckte nur einen
Teil ab und war zudem case-sensitiv – die Folge war, dass bei der
Registrierung kein Entwickler vorbelegt und beim Versand der
Bewertungsanforderung kein Empfänger ermittelt werden konnte.

Zusätzlich wurde in ``notify_bericht_bewertung_batch`` ``b.get(...)``
auf einem ``sqlite3.Row`` aufgerufen – ``sqlite3.Row`` kennt keine
``.get``-Methode, der ``AttributeError`` wurde von der Cognos-Bulk-Route
verschluckt und keine E-Mail ging raus.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

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


def _seed_person(db, *, user_id, vorname, nachname, ad_name=None, email=None):
    db.execute(
        "INSERT INTO persons (user_id, vorname, nachname, ad_name, email, aktiv) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (user_id, vorname, nachname, ad_name, email),
    )
    db.commit()


class ResolvePersonByEigentuemerTests(unittest.TestCase):
    """Variantenreiches Matching auf der ``persons``-Tabelle."""

    def setUp(self):
        self.db = _open_db()
        _seed_person(
            self.db, user_id="mmuster", vorname="Max", nachname="Muster",
            ad_name="MMuster", email="max.muster@example.org",
        )

    def _resolve(self, value):
        from webapp.routes.cognos import _resolve_person_by_eigentuemer
        return _resolve_person_by_eigentuemer(self.db, value)

    def test_plain_login(self):
        row = self._resolve("mmuster")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_domain_login_prefix(self):
        row = self._resolve("BANK\\mmuster")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_case_insensitive(self):
        # Cognos liefert teilweise Großschreibung, persons hat Kleinschreibung
        row = self._resolve("MMUSTER")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_full_name_lastname_first(self):
        row = self._resolve("Muster, Max")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_full_name_firstname_first(self):
        row = self._resolve("Max Muster")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_login_in_parentheses(self):
        row = self._resolve("Muster, Max (mmuster)")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_name_in_parentheses(self):
        row = self._resolve("mmuster (Max Muster)")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_whitespace_is_stripped(self):
        row = self._resolve("  mmuster  ")
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], "mmuster")

    def test_empty_returns_none(self):
        self.assertIsNone(self._resolve(""))
        self.assertIsNone(self._resolve(None))
        self.assertIsNone(self._resolve("   "))

    def test_no_match_returns_none(self):
        self.assertIsNone(self._resolve("Frau, Unbekannt"))

    def test_inactive_person_is_ignored(self):
        self.db.execute("UPDATE persons SET aktiv=0 WHERE user_id='mmuster'")
        self.db.commit()
        self.assertIsNone(self._resolve("mmuster"))


class NotifyBerichtBewertungBatchRowAccessTests(unittest.TestCase):
    """Regressionstest: ``notify_bericht_bewertung_batch`` darf nicht mehr
    ``b.get(...)`` auf ``sqlite3.Row`` aufrufen – das wirft ``AttributeError``
    und verhindert den E-Mail-Versand komplett.
    """

    def setUp(self):
        self.db = _open_db()
        # notify_enabled_bewertung muss aktiv sein, sonst kehrt die Funktion
        # ohnehin früh zurück – wir wollen den Code-Pfad treffen, der die
        # Eigentümer-Spalte liest.
        self.db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) "
            "VALUES ('notify_enabled_bewertung', '1')"
        )
        self.db.execute(
            "INSERT INTO cognos_berichte (berichtsname, suchpfad, eigentuemer) "
            "VALUES (?, ?, ?)",
            ("Demo-Bericht", "Pfad / Demo", "Muster, Max (mmuster)"),
        )
        self.db.commit()

    def test_row_access_does_not_raise(self):
        from webapp import email_service
        bericht_rows = self.db.execute("SELECT * FROM cognos_berichte").fetchall()
        # Stellt sicher, dass wir wirklich sqlite3.Row übergeben (kein dict).
        self.assertIsInstance(bericht_rows[0], sqlite3.Row)

        with patch.object(email_service, "send_mail", return_value=True) as send:
            ok = email_service.notify_bericht_bewertung_batch(
                self.db, bericht_rows, "empfaenger@example.org",
                base_url="https://idv.example.org",
            )
        self.assertTrue(ok)
        send.assert_called_once()
        # Bericht-spezifischer Link statt generischer /cognos/-Listenseite
        _, _, _, html, _text = send.call_args[0] + (None,) * (5 - len(send.call_args[0]))
        # send_mail-Signatur: (db, recipient, subject, html, text)
        # Wir prüfen den HTML-Body auf den highlight-Parameter
        kwargs_html = send.call_args[0][3]
        self.assertIn("highlight=", kwargs_html)


if __name__ == "__main__":
    unittest.main()
