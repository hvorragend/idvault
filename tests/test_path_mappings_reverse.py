"""Unit-Tests fuer ``scanner.path_utils.reverse_path_mappings``.

Hintergrund: Der Scanner persistiert ``full_path`` bereits gemappt
(z.B. UNC -> Anzeige-Laufwerk). Greift die Webapp spaeter direkt auf
diesen Pfad zu (Archivierung, Hash-Nachberechnung), schlaegt das fehl,
sobald der Webapp-Prozess das Mapping nicht kennt. ``reverse_path_mappings``
wandelt fuer solche Faelle die einfachen Praefix-Mappings zurueck.

Aufruf:
    python -m unittest tests.test_path_mappings_reverse
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from scanner.path_utils import (  # noqa: E402
    apply_path_mappings,
    reverse_path_mappings,
)


class ReversePathMappingsTests(unittest.TestCase):

    def test_simple_prefix_roundtrip(self):
        mappings = [{"pattern": r"\\srv01\share", "replacement": "X:"}]
        raw = r"\\srv01\share\dir\file.xlsx"
        mapped = apply_path_mappings(raw, mappings)
        self.assertEqual(mapped, r"X:\dir\file.xlsx")
        self.assertEqual(reverse_path_mappings(mapped, mappings), raw)

    def test_case_insensitive_replacement_match(self):
        # Mapping wurde mit Großschreibung definiert, der gespeicherte
        # Pfad nutzt Kleinbuchstaben — Umkehr greift trotzdem.
        mappings = [{"pattern": r"\\srv01\share", "replacement": "X:"}]
        self.assertEqual(
            reverse_path_mappings(r"x:\dir\file.xlsx", mappings),
            r"\\srv01\share\dir\file.xlsx",
        )

    def test_no_match_returns_input(self):
        mappings = [{"pattern": r"\\srv01\share", "replacement": "X:"}]
        # Pfad hat kein passendes replacement-Praefix.
        self.assertEqual(
            reverse_path_mappings(r"Y:\anders\datei.xlsx", mappings),
            r"Y:\anders\datei.xlsx",
        )

    def test_empty_inputs_pass_through(self):
        self.assertEqual(reverse_path_mappings("", [{"pattern": "a", "replacement": "b"}]), "")
        self.assertEqual(reverse_path_mappings("X:\\foo", []), "X:\\foo")
        self.assertEqual(reverse_path_mappings("X:\\foo", None), "X:\\foo")  # type: ignore

    def test_regex_mapping_skipped(self):
        # Regex-Regeln sind nicht eindeutig invertierbar -> unveraendert.
        mappings = [
            {"pattern": r"\\\\srv\d+\\share", "replacement": "X:", "regex": True},
        ]
        self.assertEqual(
            reverse_path_mappings(r"X:\dir\file.xlsx", mappings),
            r"X:\dir\file.xlsx",
        )

    def test_multiple_mappings_reverse_order(self):
        # Forward: erst /mnt -> /netz, dann /netz/team -> T:
        # Reverse muss zuerst T: -> /netz/team aufloesen, dann /netz -> /mnt.
        mappings = [
            {"pattern": "/mnt", "replacement": "/netz"},
            {"pattern": "/netz/team", "replacement": "T:"},
        ]
        raw = "/mnt/team/projekt/datei.xlsm"
        mapped = apply_path_mappings(raw, mappings)
        self.assertEqual(mapped, "T:/projekt/datei.xlsm")
        self.assertEqual(reverse_path_mappings(mapped, mappings), raw)

    def test_empty_replacement_skipped(self):
        # Ein Mapping mit leerem replacement waere nicht eindeutig
        # zurueckzubauen — die Umkehr ueberspringt es.
        mappings = [{"pattern": r"\\srv\share", "replacement": ""}]
        self.assertEqual(
            reverse_path_mappings(r"\dir\file.xlsx", mappings),
            r"\dir\file.xlsx",
        )

    def test_unicode_path_preserved(self):
        # Umlaute im Pfad duerfen die Umkehr nicht beschaedigen.
        mappings = [{"pattern": r"\\srv\share", "replacement": "Z:"}]
        self.assertEqual(
            reverse_path_mappings(r"Z:\Bericht_fuer_Quartal\datei.xlsx", mappings),
            r"\\srv\share\Bericht_fuer_Quartal\datei.xlsx",
        )


if __name__ == "__main__":
    unittest.main()
