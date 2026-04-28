"""Gemeinsam genutzte Hilfsfunktionen für webapp-Module."""

from urllib.parse import urlparse

# Dateierweiterung → IDV-Typ-Vorschlag (gespiegelt aus scanner.py)
_EXT_TO_TYP = {
    ".xlsx": "Excel-Tabelle",
    ".xlsm": "Excel-Makro",
    ".xlsb": "Excel-Makro",
    ".xls":  "Excel-Tabelle",
    ".xltm": "Excel-Makro",
    ".xltx": "Excel-Tabelle",
    ".accdb": "Access-Datenbank",
    ".mdb":   "Access-Datenbank",
    ".accde": "Access-Datenbank",
    ".accdr": "Access-Datenbank",
    ".py":    "Python-Skript",
    ".r":     "Sonstige",
    ".rmd":   "Sonstige",
    ".sql":   "SQL-Skript",
    ".pbix":  "Power-BI-Bericht",
    ".pbit":  "Power-BI-Bericht",
    ".ida":   "Cognos-Report",
}


def _idv_typ_vorschlag(extension: str, has_macros) -> str:
    ext = (extension or "").lower()
    if ext in (".xlsx", ".xls", ".xltx") and has_macros:
        return "Excel-Makro"
    return _EXT_TO_TYP.get(ext, "unklassifiziert")


def _int_or_none(val):
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


def _safe_referer_url(request, default: str) -> str:
    """Liefert eine same-origin Referer-URL als Cancel-Ziel, sonst ``default``.

    Schützt vor Open-Redirect (fremder Host) und vor Selbst-Loops, wenn die
    Seite per POST-Validierungsfehler erneut gerendert wird (Referer == aktuelle
    URL). Rückgabewert ist immer ein relativer Pfad (path?query).
    """
    ref = request.referrer
    if not ref:
        return default
    try:
        ref_p = urlparse(ref)
        cur_p = urlparse(request.url)
    except Exception:
        return default
    if ref_p.netloc and ref_p.netloc != cur_p.netloc:
        return default
    if ref_p.scheme and ref_p.scheme not in ("http", "https"):
        return default
    if ref_p.path == cur_p.path:
        return default
    rel = ref_p.path or "/"
    if ref_p.query:
        rel += "?" + ref_p.query
    return rel
