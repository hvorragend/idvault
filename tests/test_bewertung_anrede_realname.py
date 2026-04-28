"""Test fuer die Anrede in Bewertungsanforderungs-Mails.

Hintergrund
-----------
Bisher wurde im Anschreiben ``Sehr geehrte/r {ersteller}`` der Wert
direkt aus ``idv_files.file_owner`` / ``cognos_berichte.eigentuemer``
verwendet. Das ist haeufig der AD-Login (z.B. ``ABC1234``), so dass
der Empfaenger eine Mail mit ``Sehr geehrte/r ABC1234,`` erhielt.

Die Routen kennen die zugehoerige ``persons``-Zeile, weil sie ohnehin
die E-Mail-Adresse dort nachschlagen. Der ``recipient_name``-Parameter
nimmt nun ``"Vorname Nachname"`` entgegen und ersetzt damit den
Login-Fallback.
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
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) "
        "VALUES ('notify_enabled_bewertung', '1')"
    )
    conn.commit()
    return conn


def _captured_html(send_mail_mock) -> str:
    send_mail_mock.assert_called_once()
    args = send_mail_mock.call_args[0]
    # send_mail-Signatur: (db, recipient, subject, html, text)
    return args[3]


class FileBewertungBatchAnredeTests(unittest.TestCase):
    """``notify_file_bewertung_batch`` setzt den Anzeigenamen in die Anrede."""

    def setUp(self):
        self.db = _open_db()
        self.db.execute(
            "INSERT INTO idv_files (file_name, full_path, extension, file_hash, "
            "file_owner, formula_count, has_macros, status, bearbeitungsstatus) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 'active', 'Neu')",
            ("Demo.xlsx", "/share/Demo.xlsx", "xlsx", "deadbeef", "ABC1234"),
        )
        self.db.commit()
        self.file_rows = self.db.execute("SELECT * FROM idv_files").fetchall()

    def test_recipient_name_wird_in_anrede_verwendet(self):
        from webapp import email_service
        with patch.object(email_service, "send_mail", return_value=True) as send:
            ok = email_service.notify_file_bewertung_batch(
                self.db, self.file_rows, "demo@example.org",
                base_url="https://idv.example.org",
                recipient_name="Max Muster",
            )
        self.assertTrue(ok)
        html = _captured_html(send)
        self.assertIn("Sehr geehrte/r Max Muster,", html)
        self.assertNotIn("Sehr geehrte/r ABC1234,", html)

    def test_fallback_auf_file_owner_ohne_recipient_name(self):
        from webapp import email_service
        with patch.object(email_service, "send_mail", return_value=True) as send:
            ok = email_service.notify_file_bewertung_batch(
                self.db, self.file_rows, "demo@example.org",
                base_url="https://idv.example.org",
            )
        self.assertTrue(ok)
        html = _captured_html(send)
        # Ohne aufgeloesten Namen bleibt der bisherige Fallback aktiv.
        self.assertIn("Sehr geehrte/r ABC1234,", html)


class BerichtBewertungBatchAnredeTests(unittest.TestCase):
    """``notify_bericht_bewertung_batch`` setzt den Anzeigenamen in die Anrede."""

    def setUp(self):
        self.db = _open_db()
        self.db.execute(
            "INSERT INTO cognos_berichte (berichtsname, suchpfad, eigentuemer) "
            "VALUES (?, ?, ?)",
            ("Demo-Bericht", "Pfad / Demo", "Muster, Max (mmuster)"),
        )
        self.db.commit()
        self.bericht_rows = self.db.execute("SELECT * FROM cognos_berichte").fetchall()

    def test_recipient_name_wird_in_anrede_verwendet(self):
        from webapp import email_service
        with patch.object(email_service, "send_mail", return_value=True) as send:
            ok = email_service.notify_bericht_bewertung_batch(
                self.db, self.bericht_rows, "demo@example.org",
                base_url="https://idv.example.org",
                recipient_name="Max Muster",
            )
        self.assertTrue(ok)
        html = _captured_html(send)
        self.assertIn("Sehr geehrte/r Max Muster,", html)
        self.assertNotIn("Sehr geehrte/r Muster, Max (mmuster),", html)


if __name__ == "__main__":
    unittest.main()
