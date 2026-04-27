"""Unit-Tests fuer die Default-Rauschwoerter in ``webapp.similarity``.

Quartals-Marker (``Q1`` .. ``Q4``) im Dateinamen sollen bei der Token-
basierten Aehnlichkeit als Rauschen gelten — analog zur Quartals-Maske
im Versions-Fingerprint (``db.compute_version_fingerprint``). Sonst
unterscheiden sich Reports derselben Serie allein durch den Quartals-
Marker und der Score sinkt unnoetig.

Aufruf:
    python -m unittest tests.test_similarity_noise
"""

from __future__ import annotations

import os
import sys
import unittest

# Projekt-Root auf den sys.path legen.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from webapp.similarity import DEFAULT_NOISE_WORDS, _tokenize  # noqa: E402


class SimilarityNoiseDefaultsTests(unittest.TestCase):

    def setUp(self):
        self.noise = frozenset(DEFAULT_NOISE_WORDS)

    # ── 1. Quartals-Marker sind im Default-Rauschen enthalten ──
    def test_quartal_markers_are_default_noise(self):
        for q in ("q1", "q2", "q3", "q4"):
            self.assertIn(q, self.noise, f"Quartals-Token {q} fehlt im Default-Rauschen")

    # ── 2. Tokenisierung filtert Quartals-Marker ──
    def test_tokenize_drops_quartal_marker(self):
        tokens = _tokenize("Demo_Q4_Stand", self.noise)
        self.assertIn("demo", tokens)
        self.assertIn("stand", tokens)
        self.assertNotIn("q4", tokens)

    # ── 3. Jaccard zwischen zwei Quartals-Stems steigt durch Rausch-Filter ──
    # Ohne Quartals-Filter wuerde der Q-Token den Schnitt verkleinern und
    # die Vereinigung vergroessern; mit Filter haben beide Stems denselben
    # nicht-Datums-Tokensatz.
    def test_quartal_only_difference_yields_higher_overlap(self):
        a = _tokenize("Demo_Q1_Stand", self.noise)
        b = _tokenize("Demo_Q3_Stand", self.noise)
        self.assertEqual(a, b)  # nach Rauschfilter identisch

    # ── 4. Versions-Tokens bleiben gefiltert (Regression) ──
    # Sicherstellen, dass die existierenden Default-Rauschwoerter durch die
    # Quartals-Erweiterung nicht versehentlich verloren gehen.
    def test_existing_noise_words_still_filtered(self):
        for w in ("v1", "v2", "v3", "kopie", "draft", "final"):
            self.assertIn(w, self.noise)


if __name__ == "__main__":
    unittest.main()
