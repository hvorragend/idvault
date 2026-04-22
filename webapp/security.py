"""Sicherheits-Hilfsfunktionen für idvault.

Zentraler Platz für querschnittliche Security-Helper, damit die
Blueprint-Module schlank bleiben.

Enthält:
  * ``sanitize_html``  – Stored-XSS-Schutz für Rich-Text (VULN-C)
  * ``validate_upload_mime``  – Magic-Byte-Prüfung beim Datei-Upload (VULN-I)
  * ``in_clause``  – sichere Helper für ``WHERE col IN (?, ?, …)`` (VULN-L)
  * ``user_can_read_idv`` / ``user_can_write_idv`` – Ownership-Guards (VULN-E)
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Optional, Sequence

from flask import abort


# ---------------------------------------------------------------------------
# VULN-010 – Eingabelängen-/Format-Validierung
# ---------------------------------------------------------------------------

# Zentrale Längenbegrenzungen für Freitextfelder. Werden von
# ``validate_form_lengths()`` beim Annehmen von POST-Formularen
# ausgewertet. Diese Grenzen liegen deutlich über dem fachlich Üblichen
# (IDV-Bezeichnung selten > 200 Zeichen) und fangen primär DoS/
# Speicherfüll-Versuche ab.
MAX_LENGTHS: dict[str, int] = {
    "username":            128,
    "password":            256,
    "bezeichnung":         200,
    "beschreibung":      10_000,
    "kommentar":          5_000,
    "befunde":            5_000,
    "nachweise_text":    50_000,   # Quill-HTML; vor bleach begrenzen.
    "name":               200,
    "email":              254,     # RFC 5321
    "telefon":             64,
    "kuerzel":             16,
    "new_owner":          128,
    "titel":              200,
    "abbruch_kommentar":  2_000,
    "q":                  200,     # Suchfelder
}

# Feldnamen, die niemals Zeilenumbrüche enthalten dürfen (Log-Injection,
# E-Mail-Header-Injection etc.).
_SINGLE_LINE_FIELDS = {
    "username", "email", "telefon", "kuerzel", "bezeichnung",
    "new_owner", "q", "titel",
}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_form_lengths(form, *, extra: Optional[dict[str, int]] = None) -> None:
    """Prüft die deklarierten Längen für alle bekannten Felder im Formular.

    * Überschreitungen → HTTP 400 (fast-fail, nicht still trunkieren).
    * Steuerzeichen (außer TAB/CR/LF) werden unabhängig vom Feld geblockt.
    * ``extra`` erlaubt Routen-spezifische Overrides (``{"field": max}``).

    Nicht aufgeführte Felder werden *nicht* geprüft – für alles, was nicht
    in ``MAX_LENGTHS``/``extra`` steht, gilt ohnehin das globale
    ``MAX_CONTENT_LENGTH`` (32 MB) der Flask-App.
    """
    limits = dict(MAX_LENGTHS)
    if extra:
        limits.update(extra)
    for key in form.keys():
        max_len = limits.get(key)
        if max_len is None:
            continue
        value = form.get(key, "")
        if len(value) > max_len:
            abort(400, description=f"Feld '{key}' überschreitet {max_len} Zeichen.")
        if _CONTROL_CHARS_RE.search(value):
            abort(400, description=f"Feld '{key}' enthält unzulässige Steuerzeichen.")
        if key in _SINGLE_LINE_FIELDS and ("\n" in value or "\r" in value):
            abort(400, description=f"Feld '{key}' darf keinen Zeilenumbruch enthalten.")

# ---------------------------------------------------------------------------
# VULN-C – HTML-Sanitizing für Quill-Rich-Text-Felder
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = {
    "p", "br", "hr", "span", "strong", "b", "em", "i", "u", "s", "sub", "sup",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTR = {
    "a":    ["href", "title", "rel", "target"],
    "span": ["class", "style"],
    "p":    ["class", "style"],
    "li":   ["class"],
    "ul":   ["class"],
    "ol":   ["class"],
    "td":   ["colspan", "rowspan"],
    "th":   ["colspan", "rowspan"],
}
_ALLOWED_STYLES = {
    "color", "background-color", "text-align", "text-decoration", "font-weight",
    "font-style", "padding-left",
}
_ALLOWED_PROTOCOLS = {"http", "https", "mailto", "tel"}


def sanitize_html(raw: Optional[str]) -> Optional[str]:
    """Säubert HTML-Eingaben (z. B. aus Quill-Editor) gegen Stored XSS.

    Nutzt ``bleach`` wenn verfügbar; andernfalls wird *alles* HTML entfernt
    (strikter Fallback), damit selbst ohne installierte Abhängigkeit keine
    Script-Tags oder Event-Handler in die Datenbank gelangen.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    try:
        import bleach  # type: ignore
        from bleach.css_sanitizer import CSSSanitizer  # type: ignore
        css_san = CSSSanitizer(allowed_css_properties=sorted(_ALLOWED_STYLES))
        cleaned = bleach.clean(
            text,
            tags=sorted(_ALLOWED_TAGS),
            attributes=_ALLOWED_ATTR,
            protocols=sorted(_ALLOWED_PROTOCOLS),
            strip=True,
            css_sanitizer=css_san,
        )
        cleaned = bleach.linkify(cleaned, skip_tags=["pre", "code"])
        return cleaned
    except Exception:
        # Fallback: komplett escapen – kein HTML wird gerendert. Sicher, aber hässlich.
        # Besser als ungefiltert speichern.
        return html.escape(text)


# ---------------------------------------------------------------------------
# VULN-I – Datei-Upload: Magic-Byte-Prüfung (zusätzlich zur Extension)
# ---------------------------------------------------------------------------

_MAGIC_SIGNATURES: list[tuple[str, list[tuple[int, bytes]]]] = [
    # ext , [(offset, signature)]
    ("png",  [(0, b"\x89PNG\r\n\x1a\n")]),
    ("gif",  [(0, b"GIF87a"), (0, b"GIF89a")]),
    ("jpg",  [(0, b"\xff\xd8\xff")]),
    ("jpeg", [(0, b"\xff\xd8\xff")]),
    ("pdf",  [(0, b"%PDF-")]),
    ("zip",  [(0, b"PK\x03\x04"), (0, b"PK\x05\x06"), (0, b"PK\x07\x08")]),
    # Office-Formate (ab 2007) sind gezippte OOXML – PK-Signatur wie ZIP.
    ("xlsx", [(0, b"PK\x03\x04")]),
    ("xlsm", [(0, b"PK\x03\x04")]),
    ("docx", [(0, b"PK\x03\x04")]),
    # Legacy-Office (OLE2-Compound-Document)
    ("xls",  [(0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")]),
    ("doc",  [(0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")]),
    ("xlsb", [(0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), (0, b"PK\x03\x04")]),
]
_TEXT_EXTENSIONS = {"txt", "csv"}


def validate_upload_mime(fileobj, declared_ext: str) -> bool:
    """Prüft, ob der Magic-Header der Datei zur Extension passt.

    Schließt trivial polyglot-Uploads aus: ``evil.svg`` mit PNG-Endung,
    Skriptdateien getarnt als PDF, etc.

    Args:
        fileobj: Werkzeug-``FileStorage`` (oder etwas mit ``.read()`` und
                 ``.seek()``). Der Stream wird nach der Prüfung zurückgesetzt.
        declared_ext: Extension aus dem Dateinamen (kleingeschrieben, ohne Punkt).

    Returns:
        True wenn der Header zu einer erlaubten Signatur passt – oder wenn es
        sich um Plaintext (txt/csv) handelt (kein zuverlässiger Header).
    """
    ext = (declared_ext or "").lower().lstrip(".")
    if ext in _TEXT_EXTENSIONS:
        return True

    candidates = [sigs for e, sigs in _MAGIC_SIGNATURES if e == ext]
    if not candidates:
        # Unbekannte Extension → konservativ ablehnen.
        return False

    try:
        head = fileobj.read(16)
        fileobj.seek(0)
    except Exception:
        return False
    if not head:
        return False

    for sigs in candidates:
        for offset, sig in sigs:
            if head[offset:offset + len(sig)] == sig:
                return True
    return False


# ---------------------------------------------------------------------------
# VULN-L – Sicherer Helper für ``WHERE col IN (?, ?, …)``
# ---------------------------------------------------------------------------

def in_clause(values: Sequence) -> tuple[str, list]:
    """Baut ein sicheres ``IN (?, ?, …)``-Fragment.

    Leere Listen liefern ``("0", [])`` → ergibt ein always-false-Prädikat,
    ohne SQL-Syntaxfehler.

    Beispiel::

        ph_sql, ph_params = in_clause(ids)
        db.execute(f"... WHERE f.id IN ({ph_sql}) ...", params + ph_params)
    """
    vals = list(values)
    if not vals:
        return "NULL", []
    return ",".join(["?"] * len(vals)), vals


# ---------------------------------------------------------------------------
# VULN-E – Ownership-Guards für IDV-Schreiboperationen
# ---------------------------------------------------------------------------

_OWNER_COLUMNS: tuple[str, ...] = (
    "fachverantwortlicher_id",
    "idv_entwickler_id",
    "idv_koordinator_id",
    "stellvertreter_id",
)


def user_can_read_idv(db, idv_db_id: int) -> bool:
    """Darf der aktuelle Benutzer dieses IDV lesen?

    * Alle in ``_READ_ALL_ROLES`` geführten Rollen (Admin, Koordinator,
      Revision, IT-Sicherheit, IDV-Entwickler, Fachverantwortlicher):
      Lesezugriff auf alle IDVs (via ``can_read_all``).
    * Sonstige Rollen: nur auf IDVs, an denen die Person als
      Fachverantwortlicher, Entwickler, Koordinator oder Stellvertreter
      geführt wird.
    """
    from flask import session
    from .routes import can_read_all, current_person_id

    if can_read_all():
        return True
    pid = current_person_id()
    if not pid:
        return False
    cond = " OR ".join(f"{c} = ?" for c in _OWNER_COLUMNS)
    row = db.execute(
        f"SELECT 1 FROM idv_register WHERE id = ? AND ({cond}) LIMIT 1",
        (idv_db_id, *([pid] * len(_OWNER_COLUMNS))),
    ).fetchone()
    return row is not None


def user_can_write_idv(db, idv_db_id: int) -> bool:
    """Darf der aktuelle Benutzer das IDV schreiben?

    * Admin / Koordinator: immer.
    * Fachverantwortlicher / Entwickler: nur, wenn als Beteiligter
      (Fachverantwortlicher, Entwickler, Koordinator, Stellvertreter) geführt.
    """
    from .routes import can_write, can_create, current_person_id

    if can_write():
        return True
    if not can_create():
        return False
    pid = current_person_id()
    if not pid:
        return False
    cond = " OR ".join(f"{c} = ?" for c in _OWNER_COLUMNS)
    row = db.execute(
        f"SELECT 1 FROM idv_register WHERE id = ? AND ({cond}) LIMIT 1",
        (idv_db_id, *([pid] * len(_OWNER_COLUMNS))),
    ).fetchone()
    return row is not None


def ensure_can_read_idv(db, idv_db_id: int) -> None:
    """Wirft 403, wenn der Benutzer das IDV nicht lesen darf."""
    from flask import abort
    if not user_can_read_idv(db, idv_db_id):
        abort(403)


def ensure_can_write_idv(db, idv_db_id: int) -> None:
    """Wirft 403, wenn der Benutzer das IDV nicht schreiben darf."""
    from flask import abort
    if not user_can_write_idv(db, idv_db_id):
        abort(403)
