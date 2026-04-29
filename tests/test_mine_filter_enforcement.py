"""Regression: ``?mine=``-Filter auf Listenseiten ist serverseitig erzwungen.

Hintergrund: Vor dem Fix konnten User ohne Read-All-Rolle den Filter
"nur meine" durch Weglassen des ``mine``-Query-Parameters umgehen und
sahen die kompletten Listen (alle Funde / alle Cognos-Berichte). Die
Listenseiten müssen für eingeschränkte User die Owner-Restriktion
unabhängig vom URL-Parameter anwenden.

Der Test arbeitet per Quelltext-Inspektion (analog zu den anderen
Security-Regressionstests in diesem Verzeichnis), um ohne vollständigen
App-Bootstrap auszukommen.
"""
from __future__ import annotations

import os
import re
import unittest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _slice_function(src: str, def_signature: str) -> str:
    """Liefert den Quelltext-Block einer Top-Level-Funktion ab ``def_signature``
    bis vor die nächste Top-Level-Definition."""
    start = src.index(def_signature)
    rest = src[start + len(def_signature):]
    m = re.search(r"\n(?=def |@bp\.route|class )", rest)
    end = start + len(def_signature) + (m.start() if m else len(rest))
    return src[start:end]


class MineFilterEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.funde   = _read("webapp/routes/funde.py")
        self.cognos  = _read("webapp/routes/cognos.py")

    def test_funde_imports_can_read_all(self):
        self.assertIn("can_read_all", self.funde.split("\n", 30)[7] + self.funde[:2000])

    def test_cognos_imports_can_read_all(self):
        self.assertIn("can_read_all", self.cognos[:2000])

    def _assert_route_enforces_mine(self, src: str, def_signature: str):
        block = _slice_function(src, def_signature)
        # Eingeschränkter User: mine_filt wird serverseitig auf True gesetzt.
        self.assertRegex(
            block,
            r"restrict_to_mine\s*=\s*not\s+can_read_all\(\)",
            "Route prüft nicht can_read_all() für die Mine-Erzwingung.",
        )
        self.assertRegex(
            block,
            r"if\s+restrict_to_mine\s*:\s*\n\s+mine_filt\s*=\s*True",
            "Route erzwingt mine_filt=True nicht für eingeschränkte User.",
        )
        # Fallback ohne ermittelbaren Owner-Alias darf NICHT silent alle
        # Treffer freigeben — es muss "0" als WHERE-Fragment landen.
        self.assertIn(
            'where_parts.append("0")', block,
            "Fallback ohne Owner-Alias liefert nicht das No-Match-Prädikat.",
        )

    def test_funde_eingang_enforces_mine(self):
        self._assert_route_enforces_mine(
            self.funde, "def eingang_funde():"
        )

    def test_funde_list_enforces_mine(self):
        self._assert_route_enforces_mine(
            self.funde, "def list_funde():"
        )

    def test_cognos_list_enforces_mine(self):
        self._assert_route_enforces_mine(
            self.cognos, "def list_berichte():"
        )


if __name__ == "__main__":
    unittest.main()
