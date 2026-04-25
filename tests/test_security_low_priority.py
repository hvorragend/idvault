"""Regression-Tests fuer Low-Priority-Security-Ticket #406.

#406-1 – Open-Redirect-Hygiene in /login (``_safe_next``).
#406-2 – TLS-Cipher-Auswahl/min_version im ssl_utils-SSLContext.
#406-3 – ``bleach`` ist Pflichtabhaengigkeit, kein Silent-Fallback mehr.
"""
from __future__ import annotations

import os
import ssl
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# #406-1: Open-Redirect-Hygiene
# ---------------------------------------------------------------------------

class SafeNextTests(unittest.TestCase):
    def setUp(self):
        from webapp.routes.auth import _safe_next
        self._fn = _safe_next

    def test_relative_path_passes(self):
        self.assertEqual(self._fn("/dashboard"), "/dashboard")
        self.assertEqual(self._fn("/eigenentwicklung/42/details"), "/eigenentwicklung/42/details")

    def test_query_string_preserved(self):
        self.assertEqual(self._fn("/idv?id=42"), "/idv?id=42")

    def test_protocol_relative_blocked(self):
        # //evil.example/path wuerde der frueheren startswith('/')-Pruefung
        # entwischen und im Browser auf evil.example weiterleiten.
        self.assertIsNone(self._fn("//evil.example/path"))
        self.assertIsNone(self._fn("//evil.example"))

    def test_absolute_url_blocked(self):
        self.assertIsNone(self._fn("https://evil.example/x"))
        self.assertIsNone(self._fn("http://idv.example/x"))
        self.assertIsNone(self._fn("javascript:alert(1)"))
        self.assertIsNone(self._fn("data:text/html,<script>"))

    def test_non_leading_slash_blocked(self):
        self.assertIsNone(self._fn("dashboard"))
        self.assertIsNone(self._fn("../../etc/passwd"))

    def test_empty_or_none(self):
        self.assertIsNone(self._fn(""))
        self.assertIsNone(self._fn(None))


# ---------------------------------------------------------------------------
# #406-2: TLS-Cipher / min_version
# ---------------------------------------------------------------------------

class TlsHardeningTests(unittest.TestCase):
    """Wir koennen den vollstaendigen ``build_ssl_context``-Pfad nur testen,
    wenn ein gueltiges Zertifikat vorliegt. Statt ein Self-signed-Cert zu
    erzeugen, deklarieren wir die Hardening-Erwartung gegen den Quelltext
    und einen Probe-SSLContext."""

    def test_source_sets_minimum_version_and_ciphers(self):
        path = os.path.join(os.path.dirname(__file__), "..", "ssl_utils.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ctx.minimum_version", src)
        self.assertIn("set_ciphers(", src)
        self.assertIn("ECDHE+AESGCM", src)
        self.assertIn("!aNULL", src)

    def test_cipher_string_parses_on_this_platform(self):
        # Sanity: der gewaehlte Filter ist auf gangbaren OpenSSL-Versionen
        # parsebar; sonst waere die Hardening-Aussage praktisch nutzlos.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:!aNULL:!MD5:!DSS"
        )
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # Wenn beides ohne Exception durchlief, ist der Filter brauchbar.


# ---------------------------------------------------------------------------
# #406-3: bleach ist Pflicht
# ---------------------------------------------------------------------------

class BleachHardRequirementTests(unittest.TestCase):
    def test_ensure_bleach_available_passes_when_installed(self):
        from webapp import _ensure_bleach_available
        # Im Test-Setup ist bleach vorhanden – keine Exception erwartet.
        _ensure_bleach_available()

    def test_ensure_bleach_raises_when_missing(self):
        # Wir simulieren einen ImportError, indem wir ``bleach`` temporaer
        # auf None in sys.modules setzen.
        import importlib
        import webapp as _wp
        save_b = sys.modules.get("bleach")
        save_c = sys.modules.get("bleach.css_sanitizer")
        try:
            sys.modules["bleach"] = None  # ImportError beim re-import
            with self.assertRaises(RuntimeError):
                _wp._ensure_bleach_available()
        finally:
            if save_b is not None:
                sys.modules["bleach"] = save_b
            else:
                sys.modules.pop("bleach", None)
            if save_c is not None:
                sys.modules["bleach.css_sanitizer"] = save_c

    def test_sanitize_html_no_silent_fallback(self):
        # Gegenbeispiel-Test: bei vorhandenem bleach erhalten wir echtes HTML
        # zurueck (mit Tags), nicht den escapten Roh-String.
        from webapp.security import sanitize_html
        result = sanitize_html("<p>Hallo <strong>Welt</strong></p>")
        self.assertIn("<strong>", result)
        self.assertNotIn("&lt;p&gt;", result)


if __name__ == "__main__":
    unittest.main()
