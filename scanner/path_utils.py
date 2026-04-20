"""
Pfad-Hilfsfunktionen: Mapping, Blacklist, Whitelist
====================================================
Wird von eigenentwicklung_scanner.py, teams_scanner.py und der Webapp genutzt.
"""

import re
from typing import List


def apply_path_mappings(path: str, mappings: List[dict]) -> str:
    """Ersetzt Teile eines Pfads gemäß der konfigurierten Mapping-Regeln.

    Jede Regel ist ein Dict mit:
      pattern     – Zeichenkette oder Regex-Ausdruck, der ersetzt werden soll
      replacement – Ersatztext (bei regex: Gruppen-Referenzen wie \\1 erlaubt)
      regex       – bool, Standard: false → einfaches Präfix-Ersetzen
      flags       – optionale Regex-Flags als String, z.B. "i" für IGNORECASE

    Beim einfachen Modus (regex=false) wird nur ein übereinstimmendes
    Präfix (Groß-/Kleinschreibung ignoriert) ersetzt.
    """
    if not path or not mappings:
        return path

    for mapping in mappings:
        pattern = mapping.get("pattern", "")
        replacement = mapping.get("replacement", "")
        if not pattern:
            continue

        if mapping.get("regex", False):
            flags = 0
            flag_str = mapping.get("flags", "")
            if "i" in flag_str:
                flags |= re.IGNORECASE
            if "m" in flag_str:
                flags |= re.MULTILINE
            try:
                path = re.sub(pattern, replacement, path, flags=flags)
            except re.error:
                pass
        else:
            # Einfaches Präfix-Ersetzen (Groß-/Kleinschreibung ignoriert)
            if path.lower().startswith(pattern.lower()):
                path = replacement + path[len(pattern):]

    return path


def _matches_any(path: str, patterns: List[str]) -> bool:
    """Gibt True zurück, wenn der Pfad einem der Muster entspricht.

    Jedes Muster wird zunächst als Regex probiert; schlägt das Kompilieren
    fehl, wird es als einfache Teilzeichenkette behandelt.
    Alle Vergleiche sind Groß-/Kleinschreibung-unabhängig.
    """
    path_lower = path.lower()
    for pat in patterns:
        if not pat:
            continue
        try:
            if re.search(pat, path, re.IGNORECASE):
                return True
        except re.error:
            if pat.lower() in path_lower:
                return True
    return False


def should_pass_filters(path: str, blacklist: List[str], whitelist: List[str]) -> bool:
    """Gibt True zurück, wenn der Pfad verarbeitet werden soll.

    Logik:
      1. Stimmt der Pfad mit einem Blacklist-Muster überein → False (ausgeschlossen)
      2. Ist die Whitelist nicht leer UND der Pfad passt auf kein Muster → False
      3. Sonst → True
    """
    if blacklist and _matches_any(path, blacklist):
        return False
    if whitelist and not _matches_any(path, whitelist):
        return False
    return True
