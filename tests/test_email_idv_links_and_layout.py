"""Tests fuer einheitliches Mail-Layout und IDV-Doku-Link.

Hintergrund
-----------
Mails, die sich auf eine konkrete IDV beziehen, sollen einen direkten
Link zur Detail-/Doku-Seite enthalten. Beispiel: Bei
``IDV freigegeben`` waren bisher nur Titel und IDV-ID zu sehen, aber
kein Klick-Pfad zur Dokumentation.

Zusaetzlich werden alle Default-Vorlagen ueber das gemeinsame
Volksbank-CI-Geruest (Navy-Header, Amber-Akzent, Slate-Tabellen,
einheitlicher Footer) gerendert. Diese Tests pruefen das
Mindest-Markup, damit Style-Regressionen frueh auffallen.
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
    conn.executemany(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        [
            ("notify_enabled_freigabe_abgeschlossen", "1"),
            ("notify_enabled_pruefung_faellig", "1"),
            ("notify_enabled_idv_incomplete_reminder", "1"),
            ("notify_enabled_massnahme_ueberfaellig", "1"),
            ("app_base_url", "https://idvscope.example.org"),
        ],
    )
    conn.commit()
    return conn


def _insert_idv(db: sqlite3.Connection, idv_id: str = "IDV-2026-001") -> int:
    cur = db.execute(
        "INSERT INTO idv_register (idv_id, bezeichnung) VALUES (?, ?)",
        (idv_id, "Beispiel-IDV"),
    )
    return int(cur.lastrowid)


def _captured_html(send_mail_mock) -> str:
    send_mail_mock.assert_called_once()
    return send_mail_mock.call_args[0][3]


class FreigabeAbgeschlossenLinkTests(unittest.TestCase):
    """``notify_freigabe_abgeschlossen`` haengt den IDV-Doku-Link an."""

    def setUp(self):
        self.db = _open_db()
        self.idv_db_id = _insert_idv(self.db)
        row = self.db.execute(
            "SELECT id, idv_id, bezeichnung FROM idv_register WHERE id=?",
            (self.idv_db_id,),
        ).fetchone()
        self.row = row

    def test_idv_link_im_html(self):
        from webapp import email_service
        with patch.object(email_service, "send_mail", return_value=True) as send:
            email_service.notify_freigabe_abgeschlossen(
                self.db, self.row, ["empfaenger@example.org"]
            )
            html = _captured_html(send)
        expected_url = f"https://idvscope.example.org/eigenentwicklung/{self.idv_db_id}"
        self.assertIn(expected_url, html)
        self.assertIn("Zur IDV-Doku", html)

    def test_kein_link_ohne_base_url(self):
        from webapp import email_service
        self.db.execute("DELETE FROM app_settings WHERE key='app_base_url'")
        self.db.commit()
        with patch.object(email_service, "send_mail", return_value=True) as send:
            email_service.notify_freigabe_abgeschlossen(
                self.db, self.row, ["empfaenger@example.org"]
            )
            html = _captured_html(send)
        self.assertNotIn("Zur IDV-Doku", html)


class PruefungFaelligLinkTests(unittest.TestCase):
    def test_idv_link_im_html(self):
        from webapp import email_service
        db = _open_db()
        idv_db_id = _insert_idv(db)
        row = db.execute(
            "SELECT id, idv_id, bezeichnung, '2026-01-01' AS naechste_pruefung "
            "FROM idv_register WHERE id=?", (idv_db_id,),
        ).fetchone()
        with patch.object(email_service, "send_mail", return_value=True) as send:
            email_service.notify_review_due(db, row, ["e@example.org"])
            html = _captured_html(send)
        self.assertIn(f"/eigenentwicklung/{idv_db_id}", html)


class IdvIncompleteLinkTests(unittest.TestCase):
    def test_idv_link_im_html(self):
        from webapp import email_service
        db = _open_db()
        idv_db_id = _insert_idv(db)
        row = db.execute(
            "SELECT id AS idv_db_id, idv_id, bezeichnung "
            "FROM idv_register WHERE id=?", (idv_db_id,),
        ).fetchone()
        with patch.object(email_service, "send_mail", return_value=True) as send:
            email_service.notify_idv_incomplete(
                db, row, score=40, missing=["Bezeichnung", "Eigentuemer"],
                recipient_emails=["e@example.org"],
            )
            html = _captured_html(send)
        self.assertIn(f"/eigenentwicklung/{idv_db_id}", html)


class MassnahmeUeberfaelligLinkTests(unittest.TestCase):
    def test_idv_link_und_idv_id_im_html(self):
        from webapp import email_service
        db = _open_db()
        idv_db_id = _insert_idv(db, "IDV-2026-042")
        cur = db.execute(
            "INSERT INTO massnahmen (idv_id, titel, faellig_am, status) "
            "VALUES (?, 'Demo-Massnahme', '2026-01-01', 'Offen')",
            (idv_db_id,),
        )
        massnahme_row = db.execute(
            "SELECT id, titel, faellig_am FROM massnahmen WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        with patch.object(email_service, "send_mail", return_value=True) as send:
            email_service.notify_measure_overdue(db, massnahme_row, ["e@example.org"])
            html = _captured_html(send)
        self.assertIn(f"/eigenentwicklung/{idv_db_id}", html)
        self.assertIn("IDV-2026-042", html)
        self.assertIn("Beispiel-IDV", html)


class EinheitlichesLayoutTests(unittest.TestCase):
    """Alle Default-Bodies tragen Header, Akzent-Streifen und Footer."""

    def test_alle_default_bodies_im_volksbank_geruest(self):
        from webapp import email_service as es
        bodies = {k: v for k, v in es._DEFAULTS.items() if k.endswith("_body")}
        self.assertGreaterEqual(len(bodies), 9)
        for key, html in bodies.items():
            with self.subTest(template=key):
                # Navy-Header (Volksbank-CI-Akzent)
                self.assertIn("#152342", html)
                # Amber-Akzentstreifen (Markenfarbe der Sidebar)
                self.assertIn("#f59e0b", html)
                # Einheitlicher Footer
                self.assertIn(
                    "Diese Nachricht wurde automatisch von IDVScope gesendet.",
                    html,
                )


if __name__ == "__main__":
    unittest.main()
