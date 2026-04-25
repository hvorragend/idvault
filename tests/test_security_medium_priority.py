"""Regression-Tests fuer Medium-Priority-Security-Tickets #400-#405.

#400 – ProxyFix + SESSION_COOKIE_SECURE/HSTS hinter TLS-terminierendem Proxy.
#401 – Silent-Release-Token mit serverseitigem jti (One-Time-Magic-Link).
#402 – ``scanner_db_importieren`` Pfad-Containment.
#403 – LDAPS ``ssl_verify`` nur noch via config.json-Override deaktivierbar.
#405 – Zip-Bomb-Schutz im Scanner und beim Cognos-Upload.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# #400: ProxyFix-Effekt auf SESSION_COOKIE_SECURE/HSTS
# ---------------------------------------------------------------------------

class ProxyFixWiringTests(unittest.TestCase):
    """Liest die Logik in webapp/__init__.py via Quelltext-Inspektion, damit
    der Test ohne vollstaendigen App-Bootstrap (DB, Alembic etc.) auskommt.
    Das ist genug, um die Anwesenheit der drei Schluesselzeilen zu verankern.
    """

    def setUp(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "webapp", "__init__.py"
        )
        with open(path, encoding="utf-8") as f:
            self.src = f.read()

    def test_proxyfix_imported_when_behind_proxy(self):
        self.assertIn("ProxyFix(", self.src)
        self.assertIn("IDV_BEHIND_HTTPS_PROXY", self.src)

    def test_session_cookie_secure_uses_effective_https(self):
        self.assertIn("SESSION_COOKIE_SECURE=_effective_https", self.src)

    def test_csrf_ssl_strict_reactivated(self):
        self.assertIn("WTF_CSRF_SSL_STRICT=_effective_https", self.src)

    def test_hsts_uses_effective_https(self):
        self.assertIn("IDV_EFFECTIVE_HTTPS", self.src)


# ---------------------------------------------------------------------------
# #401: Silent-Release-Token traegt jti, verify-Logik prueft revoked_at
# ---------------------------------------------------------------------------

class SilentReleaseTokenJtiTests(unittest.TestCase):
    def test_make_token_requires_jti(self):
        from webapp.tokens import make_silent_release_token, verify_silent_release_token
        tok = make_silent_release_token("k" * 32, 7, 99, "abc-jti")
        payload = verify_silent_release_token("k" * 32, tok)
        self.assertEqual(payload, {"i": 7, "p": 99, "j": "abc-jti"})

    def test_signature_changed_for_jti_param(self):
        import inspect
        from webapp.tokens import make_silent_release_token
        sig = inspect.signature(make_silent_release_token)
        self.assertIn("jti", sig.parameters)


# ---------------------------------------------------------------------------
# #402: scanner_db_importieren – Pfad-Containment
# ---------------------------------------------------------------------------

class ScannerDbImportPathTests(unittest.TestCase):
    """Testet die Pfad-Validierung ohne Flask-Request-Kontext.

    ``_validate_scanner_db_src_path`` greift auf ``current_app.instance_path``
    zu – wir setzen dafuer einen Mini-Test-Flask-Kontext per ``app.test_request_context``.
    """

    def setUp(self):
        from flask import Flask
        self._tmp = tempfile.mkdtemp(prefix="idv-test-")
        self._app = Flask(__name__)
        self._app.instance_path = self._tmp
        os.makedirs(os.path.join(self._tmp, "scanner_imports"), exist_ok=True)
        # Eine harmlose Quelldatei innerhalb des Erlaubt-Verzeichnisses
        self._ok_path = os.path.join(self._tmp, "scanner_imports", "good.db")
        with open(self._ok_path, "wb") as f:
            f.write(b"SQLite format 3\x00")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _validate(self, raw):
        from webapp.routes.admin.scanner import _validate_scanner_db_src_path
        with self._app.app_context():
            return _validate_scanner_db_src_path(raw)

    def test_valid_path_accepted(self):
        path, err = self._validate(self._ok_path)
        self.assertIsNone(err)
        self.assertIsNotNone(path)

    def test_etc_passwd_rejected(self):
        _, err = self._validate("/etc/passwd")
        self.assertIsNotNone(err)

    def test_traversal_rejected(self):
        _, err = self._validate(os.path.join(self._tmp, "scanner_imports", "..", "..", "etc", "passwd"))
        self.assertIsNotNone(err)

    def test_unc_path_rejected(self):
        _, err = self._validate(r"\\evil.example\share\x.db")
        self.assertIsNotNone(err)
        _, err = self._validate("//evil.example/share/x.db")
        self.assertIsNotNone(err)

    def test_empty_rejected(self):
        _, err = self._validate("")
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# #403: LDAPS ssl_verify – effektiver Wert kommt aus config_store
# ---------------------------------------------------------------------------

class LdapSslVerifyEffectiveTests(unittest.TestCase):
    def setUp(self):
        from webapp import config_store
        # config_store cached den config.json-Inhalt einmalig; wir patchen
        # die get_bool-Funktion direkt, das ist robust genug fuer den Test.
        self._orig_get_bool = config_store.get_bool
        self._cs = config_store

    def tearDown(self):
        self._cs.get_bool = self._orig_get_bool

    def _patch(self, value: bool):
        def _stub(key, default=False):
            if key == "IDV_LDAP_INSECURE_TLS":
                return value
            return self._orig_get_bool(key, default)
        self._cs.get_bool = _stub

    def test_default_is_strict(self):
        self._patch(False)
        from webapp.ldap_auth import ldap_ssl_verify_effective
        self.assertTrue(ldap_ssl_verify_effective())

    def test_override_disables_check(self):
        self._patch(True)
        from webapp.ldap_auth import ldap_ssl_verify_effective
        self.assertFalse(ldap_ssl_verify_effective())

    def test_admin_form_does_not_lower_ssl_verify(self):
        # POST-Handler darf ssl_verify nicht mehr stillschweigend auf 0 senken.
        with open(
            os.path.join(os.path.dirname(__file__), "..", "webapp", "routes",
                         "admin", "ldap.py"),
            encoding="utf-8",
        ) as f:
            src = f.read()
        # Genau eine Zuweisung 'ssl_verify = 1' soll uebrig bleiben (Konstante).
        self.assertIn("ssl_verify = 1", src)
        # Alte UI-Logik (Form-Toggle) ist entfernt.
        self.assertNotIn(
            'ssl_verify = 1 if request.form.get("ssl_verify") else 0',
            src,
        )


# ---------------------------------------------------------------------------
# #405: Zip-Bomb-Schutz
# ---------------------------------------------------------------------------

class ZipBombScannerTests(unittest.TestCase):
    def test_oversized_entry_blocked(self):
        from scanner.network_scanner import (
            _safe_open_zip_entry, _ZipBombSuspected, _OOXML_MAX_ENTRY_BYTES,
        )
        # Wir patchen das Limit fuer den Test, damit kein 50 MB-Eintrag noetig ist.
        import scanner.network_scanner as ns
        orig = ns._OOXML_MAX_ENTRY_BYTES
        try:
            ns._OOXML_MAX_ENTRY_BYTES = 1024  # 1 KB
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("xl/workbook.xml", b"x" * 5000)
            buf.seek(0)
            with zipfile.ZipFile(buf) as zf:
                with self.assertRaises(_ZipBombSuspected):
                    with _safe_open_zip_entry(zf, "xl/workbook.xml"):
                        pass
        finally:
            ns._OOXML_MAX_ENTRY_BYTES = orig

    def test_size_budget_rejects_aggregated_oversize(self):
        from scanner import network_scanner as ns
        orig_total = ns._OOXML_MAX_TOTAL_BYTES
        try:
            ns._OOXML_MAX_TOTAL_BYTES = 4096
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("a.xml", b"a" * 3000)
                zf.writestr("b.xml", b"b" * 3000)
            buf.seek(0)
            with zipfile.ZipFile(buf) as zf:
                ok, reason = ns._ooxml_size_budget(zf)
            self.assertFalse(ok)
            self.assertIn("Gesamtgroesse", reason)
        finally:
            ns._OOXML_MAX_TOTAL_BYTES = orig_total

    def test_size_budget_passes_normal_xlsx(self):
        from scanner import network_scanner as ns
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", b"<root/>")
            zf.writestr("xl/worksheets/sheet1.xml", b"<sheet/>")
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            ok, reason = ns._ooxml_size_budget(zf)
        self.assertTrue(ok, reason)


class ZipBombCognosUploadTests(unittest.TestCase):
    def test_high_compression_ratio_rejected(self):
        from webapp.routes.cognos import _xlsx_zip_bomb_suspect
        # 1 MB nullen deflate-en auf wenige Bytes – krasses Verhaeltnis.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("payload.xml", b"\x00" * (5 * 1024 * 1024))
        reason = _xlsx_zip_bomb_suspect(buf.getvalue())
        self.assertIsNotNone(reason)

    def test_real_workbook_passes(self):
        from webapp.routes.cognos import _xlsx_zip_bomb_suspect
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/workbook.xml", b"<workbook>" + b"<x/>" * 50 + b"</workbook>")
        self.assertIsNone(_xlsx_zip_bomb_suspect(buf.getvalue()))


if __name__ == "__main__":
    unittest.main()
