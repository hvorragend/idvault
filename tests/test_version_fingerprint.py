"""Unit-Tests fuer ``db.compute_version_fingerprint`` (Issue #359).

Der Fingerprint identifiziert wiederkehrende Versionen derselben Datei in
einem Ordner (Reports, Kalkulationen, Statistiken …) und dient als dritter
Auto-Link-Pfad neben SHA-256-Hashdublette und Similarity-Score.

Die Tests laufen ohne Datenbank — nur die reine Funktion. Aufruf:
    python -m unittest tests.test_version_fingerprint
"""

from __future__ import annotations

import os
import sys
import unittest

# Projekt-Root auf den sys.path legen (db.py liegt im Wurzelverzeichnis).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from db import compute_version_fingerprint  # noqa: E402


class ComputeVersionFingerprintTests(unittest.TestCase):

    # ── 1. Quartals-Reports: gleiche Serie → identischer Fingerprint ──
    def test_quartal_collapse_to_same_fingerprint(self):
        a = compute_version_fingerprint(
            r"X:\Finanzen\Berichte\Report_2024Q1.xlsx", "Report_2024Q1.xlsx"
        )
        b = compute_version_fingerprint(
            r"X:\Finanzen\Berichte\Report_2024Q2.xlsx", "Report_2024Q2.xlsx"
        )
        c = compute_version_fingerprint(
            r"X:\Finanzen\Berichte\Report_2025Q1.xlsx", "Report_2025Q1.xlsx"
        )
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        # Ordner ist Teil des Fingerprints (lower-case)
        self.assertIn(r"x:\finanzen\berichte", a)
        self.assertIn("|report_####q#", a)

    # ── 2. ISO-Datum im Stem ──
    def test_iso_date_masked(self):
        a = compute_version_fingerprint(
            "/srv/share/Tagesabschluss_2024-06-15.xlsx",
            "Tagesabschluss_2024-06-15.xlsx",
        )
        b = compute_version_fingerprint(
            "/srv/share/Tagesabschluss_2024-06-16.xlsx",
            "Tagesabschluss_2024-06-16.xlsx",
        )
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertIn("####-##-##", a)
        # Jahr wurde als Teil der ISO-Maske erfasst (nicht doppelt maskiert).
        self.assertNotIn("####-##-##|", a)  # Stem-Teil enthaelt das Datum, nicht den Trenner
        self.assertTrue(a.endswith("tagesabschluss_####-##-##"))

    # ── 2b. Deutsches Datum DD.MM.YYYY ──
    # Reports mit deutschem Tagesdatum im Namen (z.B. quartalsweise abgelegte
    # Stand-Reports mit wechselndem Stichtag) muessen kollabieren — auch
    # wenn der Tag groesser als 12 ist und damit nicht durch die generische
    # Monatsmaske abgedeckt wird.
    def test_de_date_masked(self):
        # Tagesvarianten: drei mit Tag>12 (umgeht das Monatsmuster) und
        # eine mit Tag<=12 (Sanity: kollabiert ueber dieselbe Maske).
        a = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Stand 27.04.2024.xlsx",
            "Demo_Stand 27.04.2024.xlsx",
        )
        b = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Stand 13.05.2024.xlsx",
            "Demo_Stand 13.05.2024.xlsx",
        )
        c = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Stand 30.11.2024.xlsx",
            "Demo_Stand 30.11.2024.xlsx",
        )
        d = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Stand 05.07.2024.xlsx",
            "Demo_Stand 05.07.2024.xlsx",
        )
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, d)
        self.assertTrue(a.endswith("demo_stand ##.##.####"))

    # ── 2c. DE-Datum kombiniert mit Quartal ──
    # Beide Masken laufen, der Quartals-Token sowie das Tagesdatum kollabieren.
    def test_de_date_with_quartal_masked(self):
        a = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Q1_Stand 27.04.2024.xlsx",
            "Demo_Q1_Stand 27.04.2024.xlsx",
        )
        b = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Q2_Stand 13.05.2024.xlsx",
            "Demo_Q2_Stand 13.05.2024.xlsx",
        )
        c = compute_version_fingerprint(
            r"X:\Beispiel\Demo_Q3_Stand 30.11.2024.xlsx",
            "Demo_Q3_Stand 30.11.2024.xlsx",
        )
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertTrue(a.endswith("demo_q#_stand ##.##.####"))

    # ── 2d. DE-Datum darf nicht laengere Ziffernfolgen anschneiden ──
    # Ein 5-stelliger Zahlenblock ist kein Tagesdatum — die Datumsmaske darf
    # ihn nicht anschneiden.
    def test_de_date_does_not_match_longer_digit_runs(self):
        fp = compute_version_fingerprint(
            "/data/Demo_12345.06.2024.csv",
            "Demo_12345.06.2024.csv",
        )
        self.assertIsNotNone(fp)
        # Tagesdatum-Maske darf den 5-stelligen Block nicht angreifen;
        # nur Jahr (####) und Monat (##) werden ueber die generischen
        # Masken abgedeckt.
        self.assertNotIn("##.##.####", fp)

    # ── 3. Monat als eigenstaendiges Token ──
    def test_month_token_masked(self):
        a = compute_version_fingerprint(
            r"\\fs\share\Stat_07.xlsx", "Stat_07.xlsx"
        )
        b = compute_version_fingerprint(
            r"\\fs\share\Stat_08.xlsx", "Stat_08.xlsx"
        )
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("stat_##"))

    # ── 4. Versions-Suffix v1, v10 ──
    def test_version_suffix_masked(self):
        a = compute_version_fingerprint(
            "/data/Konzept_v1.docx", "Konzept_v1.docx"
        )
        b = compute_version_fingerprint(
            "/data/Konzept_v10.docx", "Konzept_v10.docx"
        )
        c = compute_version_fingerprint(
            "/data/Konzept_V42.docx", "Konzept_V42.docx"
        )
        self.assertEqual(a, b)
        # Case-insensitiv: V42 wird ebenfalls maskiert und matcht.
        self.assertEqual(a, c)
        self.assertTrue(a.endswith("konzept_v#"))

    # ── 5. Dreistellige Sequenz _001, _042 ──
    def test_three_digit_sequence_masked(self):
        a = compute_version_fingerprint(
            "/data/Lauf_001.csv", "Lauf_001.csv"
        )
        b = compute_version_fingerprint(
            "/data/Lauf_042.csv", "Lauf_042.csv"
        )
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("lauf_###"))

    # ── 6. Kopie-Suffix ist KEINE Versionsmaske ──
    # ``Report - Kopie.xlsx`` darf nicht denselben Fingerprint haben wie
    # ``Report.xlsx`` — sonst wuerde eine versehentliche Datei-Kopie
    # automatisch als Versions-Treffer der IDV zugeschlagen.
    def test_copy_suffix_keeps_distinct_fingerprint(self):
        a = compute_version_fingerprint(
            r"X:\Finanzen\Report.xlsx", "Report.xlsx"
        )
        b = compute_version_fingerprint(
            r"X:\Finanzen\Report - Kopie.xlsx", "Report - Kopie.xlsx"
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a, b)

    # ── 7. Nicht-Versionsdatei: keine Maske greift, Fingerprint stabil ──
    def test_non_version_filename_returns_stable_fingerprint(self):
        a = compute_version_fingerprint(
            "/data/Hauptkalkulation.xlsx", "Hauptkalkulation.xlsx"
        )
        self.assertIsNotNone(a)
        self.assertTrue(a.endswith("|hauptkalkulation"))
        # Idempotent: derselbe Pfad/Name -> derselbe Fingerprint.
        self.assertEqual(
            a,
            compute_version_fingerprint(
                "/data/Hauptkalkulation.xlsx", "Hauptkalkulation.xlsx"
            ),
        )

    # ── 8. Fallback-Guard: zu wenig Restzeichen nach Maskierung ──
    # Stem ``2024.xlsx`` wuerde nach Jahres-Maske komplett zu ``####``
    # kollabieren — ohne Guard wuerde dann jede ``20xx.xlsx``-Datei im
    # Ordner derselben IDV zugeordnet. Erwartet: None.
    def test_too_short_after_masking_returns_none(self):
        self.assertIsNone(
            compute_version_fingerprint("/data/2024.xlsx", "2024.xlsx")
        )
        # Auch ein reines Quartals-Stem ist zu unspezifisch.
        self.assertIsNone(
            compute_version_fingerprint("/data/Q1.xlsx", "Q1.xlsx")
        )

    # ── 9. Verschiedene Ordner -> verschiedene Fingerprints ──
    # (Pfadsensitiv: Umstrukturierungen werden nicht automatisch nachgezogen.)
    def test_path_sensitive(self):
        a = compute_version_fingerprint(
            r"X:\A\Report_2024Q1.xlsx", "Report_2024Q1.xlsx"
        )
        b = compute_version_fingerprint(
            r"X:\B\Report_2024Q1.xlsx", "Report_2024Q1.xlsx"
        )
        self.assertNotEqual(a, b)

    # ── 10. Leere/None-Eingaben ──
    def test_empty_inputs_return_none(self):
        self.assertIsNone(compute_version_fingerprint(None, "x.xlsx"))
        self.assertIsNone(compute_version_fingerprint("/x.xlsx", None))
        self.assertIsNone(compute_version_fingerprint("", ""))

    # ── 11. Case-Insensitivitaet des Fingerprints ──
    def test_case_insensitive(self):
        a = compute_version_fingerprint(
            r"X:\Foo\REPORT_2024Q1.xlsx", "REPORT_2024Q1.xlsx"
        )
        b = compute_version_fingerprint(
            r"x:\foo\report_2024q1.xlsx", "report_2024q1.xlsx"
        )
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
