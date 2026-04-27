"""Tests für den manuellen Versand der Owner-Sammelbenachrichtigung
aus dem Admin-UI (force-Sofortversand und Test-Versand an eine
einzelne Adresse).

Geprüft werden die Erweiterungen an
``webapp.notification_scheduler._dispatch_owner_digest``:

* Rückgabewert ist ein Dict mit ``sent`` / ``candidates`` /
  ``skipped_test_limit``;
* ``test_recipient`` leitet die Mails an die Test-Adresse um, schreibt
  weder ``self_service_tokens`` noch ``notification_log`` und ist auf
  ``test_limit`` Empfänger begrenzt;
* ``force=True`` ignoriert den Tageslimit-Eintrag in
  ``notification_log`` und protokolliert den Versand wie ein regulärer
  Lauf;
* der Master-Switch (``self_service_enabled``) blockt nur den regulären
  Lauf, nicht den Testversand.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

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


def _insert_person(conn, user_id, email):
    cur = conn.execute(
        "INSERT INTO persons (user_id, nachname, vorname, email, aktiv) "
        "VALUES (?, 'Mustermann', 'Max', ?, 1)",
        (user_id, email),
    )
    return cur.lastrowid


def _insert_file(conn, *, file_hash, full_path, owner):
    cur = conn.execute(
        """
        INSERT INTO idv_files
            (file_hash, full_path, file_name, extension,
             file_owner, status, bearbeitungsstatus)
        VALUES (?, ?, ?, ?, ?, 'active', 'Neu')
        """,
        (
            file_hash,
            full_path,
            os.path.basename(full_path),
            os.path.splitext(full_path)[1].lstrip("."),
            owner,
        ),
    )
    return cur.lastrowid


class _StubWriter:
    """Ersetzt ``DbWriter`` im Test: führt Callbacks synchron auf der
    Test-Verbindung aus. So vermeiden wir Threading und brauchen keinen
    laufenden Worker."""

    def __init__(self, conn):
        self.conn = conn

    def submit(self, func, *, wait=False):
        func(self.conn)
        class _F:
            def result(self_inner, timeout=None):
                return None
        return _F()


class OwnerDigestManualDispatchTests(unittest.TestCase):
    """End-to-end Tests für ``_dispatch_owner_digest`` im manuellen Modus."""

    def setUp(self):
        from flask import Flask
        self.db = _open_db()
        # Self-Service per Default an, damit der Master-Switch greift.
        # Schema legt einige App-Settings bereits an → INSERT OR REPLACE.
        self.db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) "
            "VALUES ('self_service_enabled', '1')"
        )
        self.db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) "
            "VALUES ('app_base_url', 'http://localhost')"
        )
        self.db.commit()

        self.person_id = _insert_person(self.db, "muster.max", "max@example.org")
        self.file_id = _insert_file(
            self.db,
            file_hash="HASH-A",
            full_path=r"\\share\dir\datei.xlsx",
            owner="muster.max",
        )

        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "x" * 32

        self.sent_calls = []

        def _fake_send(db, *, recipient_email, recipient_name, file_rows,
                       magic_link, base_url="", burst=False, test_banner=None):
            self.sent_calls.append({
                "recipient_email": recipient_email,
                "recipient_name":  recipient_name,
                "file_count":      len(file_rows),
                "magic_link":      magic_link,
                "burst":           burst,
                "test_banner":     test_banner,
            })
            return True

        from webapp import email_service
        from webapp import notification_scheduler

        self.patches = [
            mock.patch.object(email_service, "notify_owner_digest", _fake_send),
            mock.patch.object(email_service, "get_app_base_url",
                              lambda _db: "http://localhost"),
            mock.patch.object(notification_scheduler, "get_writer",
                              lambda: _StubWriter(self.db)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _call(self, **kw):
        from webapp.notification_scheduler import _dispatch_owner_digest
        with self.app.app_context():
            return _dispatch_owner_digest(self.db, "2026-04-27", **kw)

    # ------------------------------------------------------------------
    # Rückgabe-Vertrag
    # ------------------------------------------------------------------
    def test_returns_dict_with_expected_keys(self):
        result = self._call()
        self.assertIsInstance(result, dict)
        self.assertIn("sent", result)
        self.assertIn("candidates", result)
        self.assertIn("skipped_test_limit", result)
        self.assertEqual(result["sent"], 1)

    # ------------------------------------------------------------------
    # Test-Modus
    # ------------------------------------------------------------------
    def test_test_recipient_overrides_recipient_email(self):
        result = self._call(test_recipient="admin@example.org")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(self.sent_calls), 1)
        call = self.sent_calls[0]
        self.assertEqual(call["recipient_email"], "admin@example.org")
        # Banner zeigt die ursprüngliche Adresse, damit der Admin den
        # eigentlichen Empfänger erkennt.
        self.assertIn("max@example.org", call["test_banner"])

    def test_test_recipient_uses_placeholder_link(self):
        self._call(test_recipient="admin@example.org")
        call = self.sent_calls[0]
        # Kein gültiges Token, sondern erkennbarer Platzhalter — sonst
        # könnte ein Klick einen echten Self-Service-Vorgang auslösen.
        self.assertIn("TEST-MODE-NO-VALID-TOKEN", call["magic_link"])

    def test_test_recipient_writes_no_tokens(self):
        self._call(test_recipient="admin@example.org")
        rows = self.db.execute(
            "SELECT COUNT(*) AS c FROM self_service_tokens"
        ).fetchone()
        self.assertEqual(rows["c"], 0)

    def test_test_recipient_writes_no_notification_log(self):
        self._call(test_recipient="admin@example.org")
        rows = self.db.execute(
            "SELECT COUNT(*) AS c FROM notification_log "
            "WHERE kind='owner_digest'"
        ).fetchone()
        self.assertEqual(rows["c"], 0)

    def test_test_recipient_respects_test_limit(self):
        # Vier weitere Empfänger, jeweils mit eigener Datei.
        for i in range(4):
            pid = _insert_person(
                self.db, f"user.{i}", f"user{i}@example.org"
            )
            _insert_file(
                self.db,
                file_hash=f"HASH-{i}",
                full_path=fr"\\share\dir\file_{i}.xlsx",
                owner=f"user.{i}",
            )
        self.db.commit()
        result = self._call(test_recipient="admin@example.org", test_limit=2)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["skipped_test_limit"], 3)
        self.assertEqual(len(self.sent_calls), 2)

    def test_test_recipient_bypasses_master_switch(self):
        # Master-Switch deaktivieren — der reguläre Lauf wäre damit
        # ein No-Op, der Testversand muss aber weiterhin funktionieren,
        # damit der Admin das Layout vor dem Aktivieren prüfen kann.
        self.db.execute(
            "UPDATE app_settings SET value='0' WHERE key='self_service_enabled'"
        )
        self.db.commit()
        result_regular = self._call()
        self.assertEqual(result_regular["sent"], 0)
        result_test = self._call(test_recipient="admin@example.org")
        self.assertEqual(result_test["sent"], 1)

    # ------------------------------------------------------------------
    # Force-Modus
    # ------------------------------------------------------------------
    def test_force_bypasses_existing_log_entry(self):
        # Heute wurde bereits eine Mail an genau diese Person geloggt.
        self.db.execute(
            "INSERT INTO notification_log (kind, ref_id, sent_date) "
            "VALUES ('owner_digest', ?, date('now'))",
            (self.person_id,),
        )
        self.db.commit()
        # Regulärer Lauf würde wegen Tageslimit überspringen ...
        result_regular = self._call()
        self.assertEqual(result_regular["sent"], 0)
        # ... force=True umgeht das Tageslimit.
        result_force = self._call(force=True)
        self.assertEqual(result_force["sent"], 1)

    def test_force_writes_notification_log(self):
        result = self._call(force=True)
        self.assertEqual(result["sent"], 1)
        rows = self.db.execute(
            "SELECT COUNT(*) AS c FROM notification_log "
            "WHERE kind='owner_digest' AND ref_id=?",
            (self.person_id,),
        ).fetchone()
        self.assertEqual(rows["c"], 1)


if __name__ == "__main__":
    unittest.main()
