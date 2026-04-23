"""Konfigurierbare Ähnlichkeitsanalyse für Scanner-Funde ↔ Eigenentwicklungen.

Einzige Quelle für das Scoring, das sowohl in der Funde-Liste
(``routes/funde.py``) als auch beim Verknüpfen von Dateien
(``routes/eigenentwicklung.py``) verwendet wird.

Konfiguration liegt unter ``app_settings['similarity_config']`` (JSON):
    - ``weight_type``   (int, 0–100)   Gewicht für Dateityp-Übereinstimmung
    - ``weight_owner``  (int, 0–100)   Gewicht für Besitzer/Entwickler-Match
    - ``weight_name``   (int, 0–100)   Gewicht für Namensähnlichkeit
    - ``threshold``     (int, 0–100)   Mindestpunktzahl für Anzeige
    - ``name_algorithm`` (str)          ``token_set`` | ``partial`` | ``jaccard``
    - ``noise_words``   (list[str])    Bei der Namensanalyse zu ignorierende Tokens
    - ``max_candidates`` (int)          Obergrenze Kandidaten (eigenentwicklung.py)
    - ``max_results``   (int)           Obergrenze Ergebnisliste (eigenentwicklung.py)

Für die Namensähnlichkeit wird bevorzugt ``rapidfuzz`` verwendet. Fehlt das
Paket, fällt das Modul auf ``difflib`` / reine Jaccard-Überschneidung zurück –
die App bleibt damit auch ohne die neue Dependency lauffähig.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _rf_fuzz = None
    _HAS_RAPIDFUZZ = False

from .app_settings import get_json


DEFAULT_NOISE_WORDS: list[str] = [
    "der", "die", "das", "und", "fur", "für", "von", "mit", "zu", "in",
    "v1", "v2", "v3", "final", "neu", "alt", "copy", "backup", "temp", "tmp",
    "test", "old", "new", "kopie", "entwurf", "draft",
]

DEFAULT_CONFIG: dict = {
    "weight_type":     30,
    "weight_owner":    40,
    "weight_name":     30,
    "threshold":       50,
    "name_algorithm":  "token_set",
    "noise_words":     DEFAULT_NOISE_WORDS,
    "max_candidates":  500,
    "max_results":     20,
    # Auto-Zuordnung: ab diesem Score wird ein Fund vom Batch-Endpoint
    # direkt mit dem besten IDV-Kandidaten verknüpft (Admin-Trigger).
    # Bewusst hoch gewählt (95), um false-positives auszuschließen.
    # Zusätzlich verlangt der Batch einen Plausibilitätscheck
    # (Owner-Match ODER Namensüberschneidung mit dem IDV-Bezeichner).
    "auto_assign_threshold": 95,
    # Vorschlags-Schwelle: Treffer im Band ``suggest_threshold`` ≤ Score <
    # ``auto_assign_threshold`` werden nicht automatisch verknüpft, sondern
    # dem Fachbereich im Self-Service zur Bestätigung/Ablehnung vorgelegt.
    # 0 deaktiviert die Vorschlagsphase (altes Verhalten).
    "suggest_threshold": 80,
    # Hash-Dubletten: wenn ein Fund denselben SHA-256 hat wie eine bereits
    # registrierte Datei (und die Ziel-IDV eindeutig ist), wird er als
    # Zusatz-Link verknüpft statt im Eingang zu landen. HASH_ERROR wird
    # nie als Dublette behandelt.
    "auto_link_hash_duplicates": True,
}

_NAME_ALGORITHMS = ("token_set", "partial", "jaccard")

_TOKEN_SPLIT = re.compile(r"[\W_]+")


def get_config(db) -> dict:
    """Lädt die aktive Konfiguration aus ``app_settings`` und füllt Defaults auf."""
    raw = get_json(db, "similarity_config", {}) or {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for key, default in DEFAULT_CONFIG.items():
            val = raw.get(key)
            if val is None:
                continue
            if isinstance(default, bool):
                cfg[key] = bool(val)
            elif isinstance(default, int) and not isinstance(val, bool):
                try:
                    cfg[key] = int(val)
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, list):
                if isinstance(val, list):
                    cfg[key] = [str(x).strip().lower() for x in val if str(x).strip()]
            elif isinstance(default, str):
                cfg[key] = str(val)
    if cfg["name_algorithm"] not in _NAME_ALGORITHMS:
        cfg["name_algorithm"] = DEFAULT_CONFIG["name_algorithm"]
    cfg["threshold"]    = max(0, min(100, cfg["threshold"]))
    cfg["auto_assign_threshold"] = max(cfg["threshold"], min(100, cfg["auto_assign_threshold"]))
    # Vorschlagsband: zwischen Anzeige-Mindestpunktzahl und Auto-Schwelle.
    # 0 bleibt 0 (Feature abgeschaltet).
    st = max(0, min(100, cfg["suggest_threshold"]))
    if st > 0:
        st = max(cfg["threshold"], st)
        st = min(st, cfg["auto_assign_threshold"])
    cfg["suggest_threshold"] = st
    cfg["weight_type"]  = max(0, cfg["weight_type"])
    cfg["weight_owner"] = max(0, cfg["weight_owner"])
    cfg["weight_name"]  = max(0, cfg["weight_name"])
    cfg["max_candidates"] = max(1, cfg["max_candidates"])
    cfg["max_results"]    = max(1, cfg["max_results"])
    return cfg


def rapidfuzz_available() -> bool:
    return _HAS_RAPIDFUZZ


def _tokenize(text: str, noise: frozenset[str]) -> set[str]:
    if not text:
        return set()
    parts = _TOKEN_SPLIT.split(text.lower())
    return {p for p in parts if p and p not in noise}


def _name_ratio(algorithm: str, fund_name: str, idv_name: str,
                fund_words: set[str], idv_words: set[str]) -> float:
    """Liefert einen Ähnlichkeitswert zwischen 0.0 und 1.0."""
    if not fund_words or not idv_words:
        return 0.0

    if algorithm == "jaccard" or not _HAS_RAPIDFUZZ:
        # Token-Jaccard: |∩| / |∪| – strikter als die alte max()-Variante
        # (ein 1-Token-Match auf langen Namen zählt damit weniger).
        overlap = fund_words & idv_words
        union   = fund_words | idv_words
        return len(overlap) / len(union) if union else 0.0

    # rapidfuzz-Varianten (0..100 → normalisiert auf 0..1)
    if algorithm == "partial":
        score = _rf_fuzz.partial_ratio(fund_name, idv_name)
    else:  # token_set
        score = _rf_fuzz.token_set_ratio(fund_name, idv_name)
    return score / 100.0


def score_pair(
    *,
    fund_typ: str,
    fund_owner: str,
    fund_name: str,
    idv_typ: str,
    idv_name: str,
    dev_ids_lower: Iterable[str],
    config: dict,
    noise: frozenset[str] | None = None,
) -> int:
    """Berechnet den Score für ein Fund/IDV-Paar gemäß ``config``.

    ``dev_ids_lower`` enthält die bereits kleingeschriebenen Identifikatoren
    (Kürzel, AD-Name, User-ID) des IDV-Entwicklers und Fachverantwortlichen.
    """
    if noise is None:
        noise = frozenset(config.get("noise_words") or DEFAULT_NOISE_WORDS)

    score = 0

    # 1. Typ-Match
    if fund_typ and fund_typ == idv_typ and fund_typ != "unklassifiziert":
        score += config["weight_type"]

    # 2. Besitzer/Entwickler-Match
    fund_owner_l = (fund_owner or "").lower().strip()
    if fund_owner_l:
        dev_set = {d for d in (s.lower().strip() for s in dev_ids_lower) if d}
        if fund_owner_l in dev_set:
            score += config["weight_owner"]

    # 3. Namensähnlichkeit
    weight_name = config["weight_name"]
    if weight_name > 0 and fund_name and idv_name:
        fund_stem = os.path.splitext(fund_name)[0].lower()
        idv_lower = idv_name.lower()
        fund_words = _tokenize(fund_stem, noise)
        idv_words  = _tokenize(idv_lower, noise)
        ratio = _name_ratio(
            config["name_algorithm"],
            fund_stem, idv_lower,
            fund_words, idv_words,
        )
        score += int(round(ratio * weight_name))

    return score


def is_plausible_auto_match(
    *,
    fund_typ: str,
    fund_owner: str,
    idv_typ: str,
    dev_ids_lower: Iterable[str],
) -> bool:
    """Plausibilitäts-Guard für die Auto-Zuordnung.

    Ein hoher Score allein reicht nicht: Bei freier Gewichtungs-Konfiguration
    könnte ein reiner Namenstreffer einen hohen Score ergeben, obwohl Typ
    und Eigentümer nicht passen. Wir verlangen deshalb mindestens **einen**
    harten Anker: Typ-Match (kein ``unklassifiziert``) oder Owner-Match gegen
    die Entwickler/Fachverantwortlichen-Identifikatoren.
    """
    typ_match = bool(
        fund_typ
        and fund_typ == idv_typ
        and fund_typ != "unklassifiziert"
    )
    owner_match = False
    fo = (fund_owner or "").lower().strip()
    if fo:
        dev_set = {d.lower().strip() for d in dev_ids_lower if d}
        dev_set.discard("")
        owner_match = fo in dev_set
    return typ_match or owner_match


def collect_dev_ids(person_rows: Iterable[dict]) -> set[str]:
    """Hilfsfunktion: sammelt Kürzel/AD-Name/User-ID aus Personen-Zeilen."""
    result: set[str] = set()
    for p in person_rows:
        if not p:
            continue
        for key in ("kuerzel", "ad_name", "user_id"):
            try:
                val = p[key]
            except (KeyError, IndexError, TypeError):
                val = None
            if val:
                result.add(str(val).lower().strip())
    result.discard("")
    return result
