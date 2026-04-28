"""Round-Trip-Tests für die Admin-Konfigurations-Sicherung (Issue #445).

Geprüft wird, dass

* alle vorgesehenen Tabellen ins Backup wandern,
* operationale Tabellen (``persons``, ``idv_register``, ...) explizit
  ausgespart bleiben,
* ein Restore in dieselbe (vorher modifizierte) Datenbank den
  Ausgangszustand wiederherstellt,
* FKs in ``fund_pfad_profile`` auf nicht (mehr) vorhandene Personen
  beim Restore auf ``NULL`` gesetzt werden,
* operationale Tabellen vom Restore nicht angetastet werden.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

_SCHEMA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "schema.sql")
)


def _open_db() -> sqlite3.Connection:
    """In-Memory-DB mit Projekt-Schema."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _FakeAppConfig(dict):
    pass


class _FakeApp:
    def __init__(self) -> None:
        self.config = _FakeAppConfig({"BUNDLED_VERSION": "9.9.9", "APP_VERSION": "9.9.9"})


class BackupRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        # ``current_app`` aus ``build_backup_bytes`` durch ein Stub ersetzen,
        # damit der Test ohne Flask-App-Kontext läuft.
        self._app_patch = patch(
            "webapp.routes.admin.backup.current_app", _FakeApp()
        )
        self._app_patch.start()

    def tearDown(self) -> None:
        self._app_patch.stop()

    # ──────────────────────────────────────────────────────────────────
    # Hilfen
    # ──────────────────────────────────────────────────────────────────

    def _seed_admin_config(self, conn: sqlite3.Connection) -> None:
        """Realistische Admin-Konfiguration mit Personen-Verweisen."""
        conn.executescript(
            """
            INSERT INTO persons (user_id, nachname, vorname)
              VALUES ('demo01', 'Demo', 'Anna');
            INSERT INTO persons (user_id, nachname, vorname)
              VALUES ('demo02', 'Demo', 'Bert');

            INSERT INTO org_units (bezeichnung) VALUES ('Demo-Abteilung');

            INSERT INTO geschaeftsprozesse (gp_nummer, bezeichnung, ist_kritisch)
              VALUES ('GP-DEMO-001', 'Demo-Prozess', 1);

            INSERT INTO ldap_config
              (id, enabled, server_url, base_dn, bind_dn, bind_password, user_attr)
              VALUES (1, 1, 'ldaps://demo.example.invalid', 'OU=Demo,DC=example,DC=invalid',
                      'CN=svc,DC=example,DC=invalid', 'enc::dummy', 'sAMAccountName');

            INSERT INTO ldap_group_role_mapping (group_dn, group_name, rolle, sort_order)
              VALUES ('CN=Admins,OU=Demo,DC=example,DC=invalid', 'Admins',
                      'IDV-Administrator', 1);

            INSERT INTO glossar_eintraege (begriff, beschreibung, sort_order)
              VALUES ('Demo-Begriff', 'Beschreibung des Demo-Begriffs.', 99);
            """
        )

        # fund_pfad_profile referenziert persons – deckt FK-Bereinigung ab
        conn.execute(
            """
            INSERT INTO fund_pfad_profile
              (pfad_praefix, org_unit_id, fachverantwortlicher_id,
               idv_koordinator_id, created_by_id, entwicklungsart)
              VALUES ('\\\\demo-share\\Demo\\', 1, 1, 2, 1, 'Eigenprogrammierung')
            """
        )

        # app_settings überschreiben (Seed-Insert ist bereits da)
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("smtp_host", "smtp.demo.example.invalid"),
        )
        conn.commit()

    def _seed_operational(self, conn: sqlite3.Connection) -> None:
        """Operationale Daten, die ein Restore NICHT verändern darf."""
        conn.execute(
            """
            INSERT INTO idv_register
              (idv_id, bezeichnung, status)
              VALUES ('IDV-DEMO-0001', 'Demo-Eigenentwicklung', 'In Prüfung')
            """
        )
        conn.commit()

    # ──────────────────────────────────────────────────────────────────
    # Tests
    # ──────────────────────────────────────────────────────────────────

    def test_backup_zip_contains_all_admin_tables_and_no_operational(self):
        from webapp.routes.admin import backup as backup_mod

        conn = _open_db()
        self._seed_admin_config(conn)
        self._seed_operational(conn)

        payload = backup_mod.build_backup_bytes(conn)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = set(zf.namelist())
            self.assertIn("meta.json", names)

            meta = json.loads(zf.read("meta.json").decode("utf-8"))
            self.assertEqual(meta["format_version"], backup_mod.BACKUP_FORMAT_VERSION)
            self.assertEqual(meta["app_version"], "9.9.9")

            for table in backup_mod.BACKUP_TABLES:
                self.assertIn(f"tables/{table}.json", names,
                              f"Tabelle {table} fehlt im Backup-ZIP")

            # Keine operationalen Tabellen
            for forbidden in ("persons", "idv_register", "idv_history",
                              "pruefungen", "massnahmen", "smtp_log",
                              "scan_runs", "idv_files",
                              "freigabe_pool_members"):
                self.assertNotIn(f"tables/{forbidden}.json", names,
                                 f"{forbidden} darf nicht im Backup sein")

            # Stichprobe Inhalt
            ldap_data = json.loads(zf.read("tables/ldap_config.json"))
            self.assertEqual(len(ldap_data["rows"]), 1)
            self.assertEqual(
                ldap_data["rows"][0]["server_url"],
                "ldaps://demo.example.invalid",
            )

    def test_restore_round_trip_recovers_admin_config(self):
        from webapp.routes.admin import backup as backup_mod

        conn = _open_db()
        self._seed_admin_config(conn)
        self._seed_operational(conn)

        backup_bytes = backup_mod.build_backup_bytes(conn)

        # Konfiguration mutwillig zerstören
        conn.executescript(
            """
            UPDATE ldap_config SET enabled=0, server_url='', base_dn='';
            DELETE FROM ldap_group_role_mapping;
            UPDATE app_settings SET value='changed' WHERE key='smtp_host';
            DELETE FROM glossar_eintraege WHERE begriff='Demo-Begriff';
            DELETE FROM geschaeftsprozesse WHERE gp_nummer='GP-DEMO-001';
            DELETE FROM org_units WHERE bezeichnung='Demo-Abteilung';
            """
        )
        conn.commit()

        with open("/tmp/_idv_backup_test.zip", "wb") as f:
            f.write(backup_bytes)
        try:
            _, tables = backup_mod._read_backup("/tmp/_idv_backup_test.zip")
        finally:
            os.unlink("/tmp/_idv_backup_test.zip")

        stats = backup_mod.restore_backup(conn, tables)

        # Nichts auffällig leer
        self.assertGreater(stats["org_units"], 0)
        self.assertGreater(stats["ldap_group_role_mapping"], 0)

        # Konkrete Werte zurück
        ldap = conn.execute(
            "SELECT enabled, server_url, base_dn FROM ldap_config WHERE id=1"
        ).fetchone()
        self.assertEqual(ldap["enabled"], 1)
        self.assertEqual(ldap["server_url"], "ldaps://demo.example.invalid")
        self.assertEqual(ldap["base_dn"], "OU=Demo,DC=example,DC=invalid")

        smtp = conn.execute(
            "SELECT value FROM app_settings WHERE key='smtp_host'"
        ).fetchone()
        self.assertEqual(smtp["value"], "smtp.demo.example.invalid")

        glossar = conn.execute(
            "SELECT begriff FROM glossar_eintraege WHERE begriff='Demo-Begriff'"
        ).fetchone()
        self.assertIsNotNone(glossar)

        gp = conn.execute(
            "SELECT bezeichnung FROM geschaeftsprozesse WHERE gp_nummer='GP-DEMO-001'"
        ).fetchone()
        self.assertIsNotNone(gp)

        # Operationale Tabellen unangetastet
        idv = conn.execute(
            "SELECT bezeichnung FROM idv_register WHERE idv_id='IDV-DEMO-0001'"
        ).fetchone()
        self.assertIsNotNone(idv)
        self.assertEqual(idv["bezeichnung"], "Demo-Eigenentwicklung")

        # persons unangetastet (Demo-User noch da)
        cnt = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        self.assertEqual(cnt, 2)

    def test_restore_nullifies_unknown_person_fk_in_pfad_profile(self):
        from webapp.routes.admin import backup as backup_mod

        # Quell-DB mit zwei Personen + Pfad-Profil, das beide referenziert
        src = _open_db()
        self._seed_admin_config(src)
        backup_bytes = backup_mod.build_backup_bytes(src)

        # Ziel-DB: nur eine der beiden Personen vorhanden (id=1)
        dst = _open_db()
        dst.execute(
            "INSERT INTO persons (id, user_id, nachname, vorname) "
            "VALUES (1, 'demo01', 'Demo', 'Anna')"
        )
        dst.commit()

        with open("/tmp/_idv_backup_test2.zip", "wb") as f:
            f.write(backup_bytes)
        try:
            _, tables = backup_mod._read_backup("/tmp/_idv_backup_test2.zip")
        finally:
            os.unlink("/tmp/_idv_backup_test2.zip")

        backup_mod.restore_backup(dst, tables)

        row = dst.execute(
            "SELECT fachverantwortlicher_id, idv_koordinator_id, created_by_id "
            "FROM fund_pfad_profile"
        ).fetchone()
        self.assertEqual(row["fachverantwortlicher_id"], 1)  # existiert → bleibt
        self.assertIsNone(row["idv_koordinator_id"])         # id=2 fehlt → NULL
        self.assertEqual(row["created_by_id"], 1)            # existiert → bleibt

    def test_read_backup_rejects_wrong_format_version(self):
        from webapp.routes.admin import backup as backup_mod

        path = "/tmp/_idv_backup_bad.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("meta.json", json.dumps({"format_version": 999}))
        try:
            with self.assertRaises(ValueError):
                backup_mod._read_backup(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
