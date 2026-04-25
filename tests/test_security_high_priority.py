"""Regression-Tests für High-Priority-Security-Tickets #396/#398/#399.

#396 – Privilege-Escalation: ``persons.rolle`` muss auf die ROLE_*-Allowlist
        eingeschränkt sein.
#398 – Nachweis-Download: ``full_path`` aus ``idv_files`` darf nur innerhalb
        konfigurierter Scan-Roots ausgeliefert werden.
#399 – Billion-Laughs: ``scanner/network_scanner.py`` muss XML mit
        Entity-Expansion ablehnen (defusedxml-Drop-in).
"""
from __future__ import annotations

import io
import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class NormalizeRolleTests(unittest.TestCase):
    """#396 – Allowlist für ``persons.rolle``."""

    def setUp(self):
        from webapp.routes.admin.stammdaten import _normalize_rolle, _ALLOWED_PERSON_ROLES
        self._fn = _normalize_rolle
        self._allowed = _ALLOWED_PERSON_ROLES

    def test_known_role_passes(self):
        self.assertEqual(self._fn("IDV-Administrator"), "IDV-Administrator")
        self.assertEqual(self._fn("Fachverantwortlicher"), "Fachverantwortlicher")

    def test_empty_returns_none(self):
        self.assertIsNone(self._fn(""))
        self.assertIsNone(self._fn(None))
        self.assertIsNone(self._fn("   "))

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            self._fn("Superadmin")
        with self.assertRaises(ValueError):
            self._fn("idv-administrator")  # case-sensitive


class PathWithinScanRootsTests(unittest.TestCase):
    """#398 – Nachweis-Download: Scan-Root-Whitelist."""

    def setUp(self):
        from webapp.routes.freigaben import _path_within_scan_roots
        self._fn = _path_within_scan_roots

    def test_no_roots_blocks_all(self):
        self.assertFalse(self._fn("/srv/share/file.xlsx", []))

    def test_relative_target_blocked(self):
        self.assertFalse(self._fn("relative/path.xlsx", ["/srv/share"]))

    def test_traversal_outside_root_blocked(self):
        self.assertFalse(self._fn("/etc/passwd", ["/srv/share"]))
        # Pfad-Präfix-String matchen reicht nicht: /srv/share-evil/x ist NICHT in /srv/share
        self.assertFalse(self._fn("/srv/share-evil/x", ["/srv/share"]))

    def test_exact_root_match_allowed(self):
        self.assertTrue(self._fn("/srv/share", ["/srv/share"]))

    def test_subdirectory_allowed(self):
        self.assertTrue(self._fn("/srv/share/sub/file.xlsx", ["/srv/share"]))

    def test_empty_target(self):
        self.assertFalse(self._fn("", ["/srv/share"]))
        self.assertFalse(self._fn(None, ["/srv/share"]))


class DefusedScannerXmlTests(unittest.TestCase):
    """#399 – Billion-Laughs / Quadratic-Blowup im Scanner."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scanner"))
        import scanner.network_scanner as mod
        self._ET = mod.ET

    def test_uses_defusedxml(self):
        # Drop-in-Replacement aktiv? defusedxml.ElementTree statt stdlib.
        self.assertEqual(self._ET.__name__, "defusedxml.ElementTree")

    def test_billion_laughs_payload_rejected(self):
        payload = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE lolz [\n'
            b'  <!ENTITY lol "lol">\n'
            b'  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
            b'  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
            b']>\n'
            b'<lolz>&lol2;</lolz>'
        )
        with self.assertRaises(Exception):
            self._ET.parse(io.BytesIO(payload))


if __name__ == "__main__":
    unittest.main()
