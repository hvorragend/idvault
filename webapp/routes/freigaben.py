"""Test- und Freigabeverfahren Blueprint (MaRisk AT 7.2 / DORA)

Drei Phasen:
  Phase 1 (parallel): Fachlicher Test + Technischer Test
  Phase 2 (parallel): Fachliche Abnahme + Technische Abnahme
  Phase 3 (einzeln) : Archivierung Originaldatei (revisionssicher)

Phase 2 startet erst, wenn BEIDE Phase-1-Schritte als 'Erledigt' markiert sind.
Phase 3 wird automatisch angelegt, sobald beide Phase-2-Schritte erledigt sind;
die Gesamt-Freigabe (`teststatus = 'Freigegeben'`) wird erst nach Abschluss
der Archivierung gesetzt. Wenn die Originaldatei nicht verfügbar ist (z.B.
Cognos-Berichte, die nur in agree21Analysen gespeichert sind), kann der
Schritt mit der Kennzeichnung "Datei nicht verfügbar" und verpflichtender
Begründung abgeschlossen werden.

Funktionstrennung: Entwickler der IDV darf keine Schritte abschließen.
Nur wesentliche IDVs mit wesentlicher Änderung durchlaufen dieses Verfahren.

Statuswerte (idv_freigaben.status):
  'Ausstehend' | 'Erledigt' | 'Nicht erledigt' | 'Abgebrochen'
"""
import hashlib
import os
from flask import (Blueprint, request, flash, redirect, url_for, abort,
                   session, current_app, send_from_directory, send_file, render_template, jsonify)
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from . import login_required, own_write_required, admin_required, get_db, current_person_id
from ..db_writer import get_writer
from db_write_tx import write_tx
from db import idv_completeness_score
from ..security import (sanitize_html, validate_upload_mime,
                        ensure_can_read_idv, ensure_can_write_idv,
                        in_clause)
from ..helpers import _int_or_none

bp = Blueprint("freigaben", __name__, url_prefix="/freigaben")


def _parse_combined_assignment(value):
    """Parses a combined person/pool dropdown value (e.g. '42' or 'pool_3').
    Returns (person_id, pool_id) with exactly one being non-None, or (None, None).
    """
    if not value:
        return None, None
    if isinstance(value, str) and value.startswith("pool_"):
        try:
            return None, int(value[5:])
        except ValueError:
            return None, None
    try:
        return int(value), None
    except (ValueError, TypeError):
        return None, None

_PHASE_1 = ["Fachlicher Test", "Technischer Test"]
_PHASE_2 = ["Fachliche Abnahme", "Technische Abnahme"]
_PHASE_3 = ["Archivierung Originaldatei"]
_SCHRITTE = _PHASE_1 + _PHASE_2 + _PHASE_3
_MAX_ARCHIV_UPLOAD = 256 * 1024 * 1024  # 256 MB Obergrenze für Originaldateien

# ---------------------------------------------------------------------------
# Änderungskategorie (#320): grundlegend vs. patch
# ---------------------------------------------------------------------------
# 'grundlegend' = voller 3-Phasen-Workflow (heutiges Verhalten, Default,
#                 immer bei Erstfreigabe).
# 'patch'       = verschlankter Workflow; welche Schritte entfallen, liegt
#                 in app_settings.freigabe_patch_schritte (JSON-Array).
_KATEGORIEN = ("grundlegend", "patch")
_DEFAULT_PATCH_SCHRITTE = ["Technischer Test", "Fachliche Abnahme",
                           "Archivierung Originaldatei"]


def _get_patch_schritte(db) -> list:
    """Liefert die für Patch-Freigaben konfigurierten Schritte.

    Fällt auf den konservativen Default zurück, wenn der Admin keinen
    gültigen Wert hinterlegt hat. Ungültige Einträge werden verworfen,
    damit ein vergurkter JSON-Wert den Patch-Workflow nicht öffnet.
    """
    from ..app_settings import get_json
    raw = get_json(db, "freigabe_patch_schritte", None)
    if not isinstance(raw, list) or not raw:
        return list(_DEFAULT_PATCH_SCHRITTE)
    clean = [s for s in raw if isinstance(s, str) and s in _SCHRITTE]
    return clean or list(_DEFAULT_PATCH_SCHRITTE)


def _ist_gda4_oder_dora_kritisch(db, idv_db_id: int) -> bool:
    """True, wenn die IDV nicht Patch-fähig ist.

    Sperrkriterien (FA-045 bleibt wirksam):
      * verlinkter Geschäftsprozess ist DORA-kritisch/wichtig
        (`geschaeftsprozesse.ist_kritisch = 1`), oder
      * ein erfülltes Wesentlichkeitskriterium markiert die IDV als
        GDA=4 (Abhängigkeitsgrad 4, kritische/wichtige Funktion).

    Die Textsuche im Kriteriumsnamen toleriert Umbenennungen durch den
    Admin, solange die typischen Schlüsselwörter erhalten bleiben.
    """
    row = db.execute("""
        SELECT COALESCE(gp.ist_kritisch, 0) AS ist_kritisch
          FROM idv_register r
          LEFT JOIN geschaeftsprozesse gp ON gp.id = r.gp_id
         WHERE r.id = ?
    """, (idv_db_id,)).fetchone()
    if row and row["ist_kritisch"]:
        return True
    row = db.execute("""
        SELECT 1 FROM idv_wesentlichkeit iw
          JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
         WHERE iw.idv_db_id = ? AND iw.erfuellt = 1
           AND (k.bezeichnung LIKE '%GDA%'
                OR k.bezeichnung LIKE '%Abhängigkeitsgrad 4%'
                OR k.bezeichnung LIKE '%Kritische oder wichtige%')
         LIMIT 1
    """, (idv_db_id,)).fetchone()
    return row is not None


def _get_kategorie(db, idv_db_id: int) -> str:
    """Kategorie der aktuellen Version. Fehlend/leer → 'grundlegend'."""
    row = db.execute(
        "SELECT freigabe_aenderungskategorie FROM idv_register WHERE id=?",
        (idv_db_id,)
    ).fetchone()
    if not row:
        return "grundlegend"
    k = row["freigabe_aenderungskategorie"]
    return k if k in _KATEGORIEN else "grundlegend"


def _active_phase_schritte(db, idv_db_id: int) -> tuple:
    """Liefert (phase1, phase2, phase3) – je die Schritte, die für diese
    IDV-Version tatsächlich vorgesehen sind.

    Für ``grundlegend`` entspricht das dem vollen Katalog. Für ``patch``
    wird jede Phase mit der Schnittmenge aus Admin-Konfiguration und dem
    jeweiligen Phasen-Katalog befüllt; Phasen, für die gar kein Schritt
    vorgesehen ist, sind leer und werden von den Abschluss-Helfern als
    „komplett erledigt" behandelt.
    """
    if _get_kategorie(db, idv_db_id) != "patch":
        return list(_PHASE_1), list(_PHASE_2), list(_PHASE_3)
    patch = set(_get_patch_schritte(db))
    p1 = [s for s in _PHASE_1 if s in patch]
    p2 = [s for s in _PHASE_2 if s in patch]
    p3 = [s for s in _PHASE_3 if s in patch]
    return p1, p2, p3


_WESENTLICH_SQL = """EXISTS(
    SELECT 1 FROM idv_wesentlichkeit iw
    WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
)"""

_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "xlsx", "xls", "docx", "doc",
                       "txt", "csv", "zip"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


def _upload_folder() -> str:
    folder = os.path.join(current_app.instance_path, "uploads", "freigaben")
    os.makedirs(folder, exist_ok=True)
    return folder


def _archiv_upload_folder(idv_db_id: int) -> str:
    """Zielverzeichnis für revisionssicher archivierte Originaldateien.

    Pro IDV wird ein eigener Unterordner angelegt, damit Archiv-Dateien
    klar vom Nachweis-Upload abgegrenzt sind und je IDV auditierbar bleiben.
    """
    folder = os.path.join(current_app.instance_path, "uploads", "archiv",
                          str(int(idv_db_id)))
    os.makedirs(folder, exist_ok=True)
    return folder


def _verfuegbare_scanner_dateien(db, idv_db_id: int) -> list:
    """Liefert die mit der IDV verknüpften Scanner-Dateien (Haupt- + Zusatz-Links).

    Wird im Archivierungs-Formular angeboten, damit die Originaldatei
    direkt aus dem gescannten Pfad in das Archiv übernommen werden kann
    (statt sie manuell hochzuladen).
    """
    rows = db.execute("""
        SELECT f.id, f.full_path, f.file_name, f.size_bytes,
               f.modified_at, f.file_hash
          FROM idv_files f
         WHERE f.id = (SELECT file_id FROM idv_register WHERE id = ?)
        UNION
        SELECT f.id, f.full_path, f.file_name, f.size_bytes,
               f.modified_at, f.file_hash
          FROM idv_files f
          JOIN idv_file_links lnk ON lnk.file_id = f.id
         WHERE lnk.idv_db_id = ?
        ORDER BY file_name
    """, (idv_db_id, idv_db_id)).fetchall()
    return [dict(r) for r in rows]


# Excel-Formate, die im Zellschutz-Gate der Fachlichen Abnahme auftauchen.
# OOXML-Formate (.xlsx/.xlsm/…) kann der Scanner auf Blatt-/Arbeitsmappen-
# schutz prüfen; Legacy .xls/.xlt kann er nicht zuverlässig prüfen — diese
# landen deshalb immer im Gate und müssen bewusst akzeptiert werden.
_EXCEL_OOXML_EXTS = (".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx", ".xls", ".xlt")


def _unprotected_excel_files_for_idv(db, idv_db_id: int) -> list:
    """Excel-Dateien der IDV ohne Blattschutz inkl. Akzeptanz-Status.

    Grundlage für die bewusste Fachverantwortlichen-Entscheidung während der
    Fachlichen Abnahme (MaRisk AT 7.2 / DORA). Liefert pro Datei die bereits
    erfasste Akzeptanz (Person + Zeitstempel + Begründung) oder None.

    Filter: Blattschutz fehlt (``has_sheet_protection = 0``). Ein reiner
    Workbook-Schutz (``workbook_protected``) schützt nur die Mappenstruktur,
    nicht die Zellen — solche Dateien müssen also ebenfalls bewusst
    abgenommen werden und bleiben in der Liste.
    """
    placeholders = ",".join("?" * len(_EXCEL_OOXML_EXTS))
    rows = db.execute(f"""
        SELECT f.id, f.full_path, f.file_name, f.extension, f.share_root,
               f.sheet_count, f.formula_count, f.has_macros,
               az.akzeptiert_am,
               az.begruendung,
               (p.nachname || ', ' || p.vorname) AS akzeptiert_von
          FROM idv_files f
          LEFT JOIN idv_zellschutz_akzeptanz az
                 ON az.file_id = f.id AND az.idv_db_id = ?
          LEFT JOIN persons p ON p.id = az.akzeptiert_von_id
         WHERE f.status = 'active'
           AND LOWER(f.extension) IN ({placeholders})
           AND COALESCE(f.has_sheet_protection, 0) = 0
           AND (
                f.id = (SELECT file_id FROM idv_register WHERE id = ?)
             OR f.id IN (SELECT file_id FROM idv_file_links WHERE idv_db_id = ?)
           )
         ORDER BY f.file_name
    """, (idv_db_id, *_EXCEL_OOXML_EXTS, idv_db_id, idv_db_id)).fetchall()
    return [dict(r) for r in rows]


def _save_upload(file):
    """Speichert eine hochgeladene Datei. Gibt (relativer_pfad, originaldateiname) zurück.

    Prüft Extension (VULN-I Whitelist) UND Magic-Byte-Signatur
    (VULN-I: verhindert polyglot-Uploads wie ``evil.svg`` getarnt als
    ``evil.png``). Gibt ``(None, None)`` zurück, wenn beides nicht passt.
    """
    if not file or not file.filename:
        return None, None
    if not _allowed_file(file.filename):
        return None, None
    ext = file.filename.rsplit(".", 1)[1].lower()
    if not validate_upload_mime(file.stream, ext):
        current_app.logger.warning(
            "Upload abgelehnt: Magic-Bytes passen nicht zur Extension '%s' (Datei: %s)",
            ext, file.filename,
        )
        return None, None
    original_name = file.filename
    safe_name = secure_filename(original_name) or f"upload.{ext}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
    save_name = timestamp + safe_name
    folder = _upload_folder()
    file.save(os.path.join(folder, save_name))
    return save_name, original_name


def _ist_wesentlich(db, idv_db_id: int) -> bool:
    row = db.execute(
        f"SELECT 1 FROM idv_register r WHERE r.id = ? AND {_WESENTLICH_SQL}",
        (idv_db_id,)
    ).fetchone()
    return row is not None


def _testverfahren_erforderlich(db, idv_db_id: int) -> bool:
    """Prüft ob das Testverfahren notwendig ist (wesentliche IDV + nicht 'unwesentliche Änderung')."""
    if not _ist_wesentlich(db, idv_db_id):
        return False
    row = db.execute(
        "SELECT letzte_aenderungsart FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if row and row["letzte_aenderungsart"] == "unwesentlich":
        return False
    return True


def _funktionstrennung_ok(db, idv_db_id: int, person_id: int) -> bool:
    """Admins sind ausgenommen. Entwickler darf eigene IDV nicht abschließen."""
    from . import ROLE_ADMIN
    if session.get("user_role") == ROLE_ADMIN:
        return True
    row = db.execute(
        "SELECT idv_entwickler_id FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not row or row["idv_entwickler_id"] is None:
        return True
    return row["idv_entwickler_id"] != person_id


_SOD_OVERRIDE_PREFIX = "[SoD-Ausnahme durch Administrator] "


def _is_sod_override(db, idv_db_id: int, person_id: int) -> bool:
    """True, wenn der Abschluss die Funktionstrennung übersteuert: der
    ausführende Benutzer hat die Session-Rolle ``IDV-Administrator`` und
    ist gleichzeitig als Entwickler (`idv_entwickler_id`) der betroffenen
    IDV eingetragen. In diesem Fall ist der Eingriff revisionsrelevant
    und wird im History-Eintrag eindeutig markiert.
    """
    from . import ROLE_ADMIN
    if session.get("user_role") != ROLE_ADMIN:
        return False
    if not person_id:
        return False
    row = db.execute(
        "SELECT idv_entwickler_id FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    return bool(row and row["idv_entwickler_id"] == person_id)


def _sod_log_fields(db, idv_db_id: int, person_id: int,
                    base_aktion: str, base_kommentar: str) -> tuple:
    """Liefert (aktion, kommentar) für den History-Eintrag. Im
    Admin-Override-Fall (siehe :func:`_is_sod_override`) wird die Aktion
    mit ``_sod_override`` suffixiert und der Kommentar mit einem
    sprechenden Präfix versehen, damit die Revision diese Ausnahmen ohne
    Join auf ``idv_register`` finden kann (z.B. ``SELECT * FROM
    idv_history WHERE aktion LIKE '%_sod_override'``).
    """
    if _is_sod_override(db, idv_db_id, person_id):
        return base_aktion + "_sod_override", _SOD_OVERRIDE_PREFIX + (base_kommentar or "")
    return base_aktion, base_kommentar


def _get_aktiver_stellvertreter_id(db, person_id: int):
    """Gibt stellvertreter_id zurück, wenn die Person aktuell abwesend ist."""
    row = db.execute(
        """SELECT stellvertreter_id FROM persons
           WHERE id = ? AND stellvertreter_id IS NOT NULL
             AND abwesend_bis IS NOT NULL AND abwesend_bis >= date('now')""",
        (person_id,)
    ).fetchone()
    return row["stellvertreter_id"] if row else None


def _is_pool_member(db, pool_id: int, person_id: int) -> bool:
    """True wenn person_id Mitglied des angegebenen Freigabe-Pools ist."""
    if not pool_id or not person_id:
        return False
    row = db.execute(
        "SELECT 1 FROM freigabe_pool_members WHERE pool_id = ? AND person_id = ? LIMIT 1",
        (pool_id, person_id),
    ).fetchone()
    return row is not None


def _can_complete_schritt(db, freigabe, person_id: int) -> bool:
    """Prüft ob person_id diesen Schritt abschließen/ablehnen darf.

    Admins dürfen immer. Sonst: die zugewiesene Person, deren aktiver
    Stellvertreter, oder (wenn der Schritt an einen Pool gebunden ist) jedes
    Pool-Mitglied. Phase-3 (Archivierung) ohne Zuweisung und Pool darf
    jede schreibberechtigte Person abschließen (Funktionstrennung prüft
    separat, dass kein Entwickler der IDV sich selbst archiviert).
    """
    from . import ROLE_ADMIN
    if session.get("user_role") == ROLE_ADMIN:
        return True
    zugewiesen_id = freigabe["zugewiesen_an_id"]
    if zugewiesen_id:
        if person_id == zugewiesen_id:
            return True
        if _get_aktiver_stellvertreter_id(db, zugewiesen_id) == person_id:
            return True
    pool_id = None
    try:
        pool_id = freigabe["pool_id"]
    except (KeyError, IndexError):
        pool_id = None
    if pool_id and _is_pool_member(db, pool_id, person_id):
        return True
    # Archivierung ohne Zuweisung → jeder mit Schreibrecht darf (SoD greift separat)
    if (freigabe["schritt"] in _PHASE_3
            and not zugewiesen_id and not pool_id):
        return True
    return False


def _phase1_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn ALLE für diese IDV vorgesehenen Phase-1-Schritte erledigt sind.

    Für die Patch-Kategorie (#320) kann die Menge kleiner sein (oder leer);
    eine leere Menge gilt als bereits erledigt, damit der verkürzte
    Workflow nicht hängenbleibt.
    """
    aktive, _, _ = _active_phase_schritte(db, idv_db_id)
    if not aktive:
        return True
    ph, ph_params = in_clause(aktive)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(aktive).issubset(done)


def _phase2_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn ALLE für diese IDV vorgesehenen Phase-2-Schritte erledigt sind.

    Leere Phase-2-Menge (Patch ohne Abnahmen konfiguriert) gilt als
    bereits erledigt.
    """
    _, aktive, _ = _active_phase_schritte(db, idv_db_id)
    if not aktive:
        return True
    ph, ph_params = in_clause(aktive)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(aktive).issubset(done)


def _phase3_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn der Archivierungs-Schritt als Erledigt markiert ist – oder
    wenn die Archivierung für diese IDV (Patch-Konfig) gar nicht vorgesehen
    ist und daher entfällt."""
    _, _, aktive = _active_phase_schritte(db, idv_db_id)
    if not aktive:
        return True
    ph, ph_params = in_clause(aktive)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(aktive).issubset(done)


def _ensure_archiv_schritt(conn, idv_db_id: int, person_id: int,
                            zugewiesen_an_id: int = None,
                            bearbeiter_name: str = None) -> bool:
    """Legt den Archivierungs-Schritt (Phase 3) an, sofern er noch nicht existiert.

    Idempotent und commit-frei: der Aufrufer muss bereits innerhalb einer
    write_tx(conn)-Transaktion arbeiten. Patch-Workflows ohne Archivierung
    (#320) überspringen die Anlage und geben ``False`` zurück.
    """
    _, _, aktive_p3 = _active_phase_schritte(conn, idv_db_id)
    if _PHASE_3[0] not in aktive_p3:
        return False
    existing = conn.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt=? LIMIT 1",
        (idv_db_id, _PHASE_3[0])
    ).fetchone()
    if existing:
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO idv_freigaben
            (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id)
        VALUES (?, ?, 'Ausstehend', ?, ?, ?)
    """, (idv_db_id, _PHASE_3[0], person_id, now, zugewiesen_an_id))
    conn.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
        (idv_db_id, "archivierung_beauftragt",
         "Archivierung Originaldatei beauftragt.",
         person_id, bearbeiter_name)
    )
    return True


def _finalisiere_freigabe_wenn_komplett(conn, idv_db_id: int, person_id: int,
                                         bearbeiter_name: str = None) -> bool:
    """Setzt `teststatus` und `status` auf 'Freigegeben', sobald Phase 2 UND Phase 3 komplett sind.

    Commit-frei: muss innerhalb einer umschliessenden write_tx(conn)-
    Transaktion aufgerufen werden. E-Mail-Benachrichtigung wird nicht
    mehr hier ausgeloest (wuerde den Writer-Thread auf SMTP blockieren);
    der Aufrufer muss sie nach erfolgreichem submit() separat anstossen.

    Der Workflow-Status wird automatisch auf 'Freigegeben' gesetzt, sofern
    er noch nicht in einem abgeschlossenen Zustand ist – Koordinatoren
    muessen nicht manuell eingreifen.
    """
    if not (_phase2_komplett_erledigt(conn, idv_db_id)
            and _phase3_komplett_erledigt(conn, idv_db_id)):
        return False
    row = conn.execute(
        "SELECT teststatus, status FROM idv_register WHERE id=?", (idv_db_id,)
    ).fetchone()
    if row and row["teststatus"] == "Freigegeben":
        return False
    now = datetime.now(timezone.utc).isoformat()
    # Workflow-Status automatisch auf 'Freigegeben' setzen, sofern er noch
    # nicht in einem Endzustand ist (z. B. 'Freigegeben mit Auflagen' bleibt).
    _ABGESCHLOSSENE_STATUS = {"Freigegeben", "Freigegeben mit Auflagen",
                               "Abgelehnt", "Abgekündigt", "Archiviert"}
    aktueller_status = row["status"] if row else None
    auto_freigabe = aktueller_status not in _ABGESCHLOSSENE_STATUS
    if auto_freigabe:
        conn.execute("""
            UPDATE idv_register
            SET teststatus='Freigegeben', dokumentation_vorhanden=1, aktualisiert_am=?,
                status='Freigegeben', status_geaendert_am=?, status_geaendert_von_id=?
            WHERE id=?
        """, (now, now, person_id, idv_db_id))
    else:
        conn.execute("""
            UPDATE idv_register
            SET teststatus='Freigegeben', dokumentation_vorhanden=1, aktualisiert_am=?
            WHERE id=?
        """, (now, idv_db_id))
    conn.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
        (idv_db_id, "freigabe_erteilt",
         "Alle Freigabe-Schritte (Phase 1+2+3) erledigt – Eigenentwicklung automatisch freigegeben",
         person_id, bearbeiter_name)
    )
    return True



def _ensure_test_eintraege(conn, idv_db_id: int) -> None:
    """Legt leere fachliche_testfaelle- und technischer_test-Einträge an,
    sofern noch keine existieren.

    Commit-frei: muss innerhalb einer umschliessenden write_tx(conn)-
    Transaktion aufgerufen werden.
    """
    now = datetime.now(timezone.utc).isoformat()
    fachlich_exists = conn.execute(
        "SELECT 1 FROM fachliche_testfaelle WHERE idv_id=? LIMIT 1", (idv_db_id,)
    ).fetchone()
    if not fachlich_exists:
        conn.execute("""
            INSERT INTO fachliche_testfaelle
                (idv_id, testfall_nr, beschreibung, bewertung, erstellt_am, aktualisiert_am)
            VALUES (?, 1, NULL, 'Offen', ?, ?)
        """, (idv_db_id, now, now))
    tech_exists = conn.execute(
        "SELECT 1 FROM technischer_test WHERE idv_id=? LIMIT 1", (idv_db_id,)
    ).fetchone()
    if not tech_exists:
        conn.execute("""
            INSERT INTO technischer_test
                (idv_id, ergebnis, erstellt_am, aktualisiert_am)
            VALUES (?, 'Offen', ?, ?)
        """, (idv_db_id, now, now))


# ---------------------------------------------------------------------------
# Shared helper: Freigabe-Schritt als Erledigt abschließen
# ---------------------------------------------------------------------------

def complete_freigabe_schritt(db, freigabe_id: int, person_id: int,
                               nachweise: str = None, kommentar: str = None,
                               user_name: str = None) -> bool:
    """Markiert einen ausstehenden Freigabe-Schritt als Erledigt und aktualisiert
    Phase-Status sowie IDV-Teststatus. Wird aus tests.py nach Speichern des Tests aufgerufen.

    Lauft ueber den Writer-Thread, damit die Schreibsequenz serialisiert
    wird. Read-Preflight auf `db` (Request-Reader) ist ein Mikro-Optimum;
    die finale Pruefung `status='Ausstehend'` erfolgt erneut innerhalb
    der Transaktion, damit Races zwischen zwei Abschluss-Klicks nicht zu
    doppelten Erledigt-Markierungen fuehren.

    Vor dem Abschluss werden Funktionstrennung und Zuständigkeit geprüft
    (sonst würde der Testformular-Pfad den SoD-Guard der direkten
    Abschluss-Route umgehen). Gibt ``True`` zurück, wenn der Schritt
    erfolgreich als Erledigt markiert wurde, sonst ``False``.
    """
    now = datetime.now(timezone.utc).isoformat()
    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        return False

    idv_db_id = freigabe["idv_id"]
    schritt = freigabe["schritt"]

    # Funktionstrennung: Entwickler der IDV darf den Schritt nicht
    # abschließen (Admins werden innerhalb _funktionstrennung_ok
    # durchgelassen, der SoD-Override wird in _sod_log_fields markiert).
    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        return False
    # Zuweisung: nur die zugewiesene Person, deren aktiver Stellvertreter
    # oder ein Pool-Mitglied (bzw. Admin) dürfen abschließen.
    if not _can_complete_schritt(db, freigabe, person_id):
        return False
    user_name = session.get("user_name", "") or None
    hist_aktion, hist_kommentar = _sod_log_fields(
        db, idv_db_id, person_id,
        "freigabe_schritt_erledigt", f"{schritt} erledigt",
    )

    def _do(c):
        row = c.execute(
            "SELECT status FROM idv_freigaben WHERE id=?", (freigabe_id,)
        ).fetchone()
        if not row or row["status"] != "Ausstehend":
            return False, False, False
        with write_tx(c):
            c.execute("""
                UPDATE idv_freigaben
                SET status='Erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
                    kommentar=?, nachweise_text=?
                WHERE id=?
            """, (person_id, now, kommentar, nachweise, freigabe_id))
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, hist_aktion, hist_kommentar, person_id, user_name),
            )
            freigegeben = False
            archiv_neu  = False
            if schritt in _PHASE_2 and _phase2_komplett_erledigt(c, idv_db_id):
                archiv_neu  = _ensure_archiv_schritt(c, idv_db_id, person_id, bearbeiter_name=user_name)
                freigegeben = _finalisiere_freigabe_wenn_komplett(c, idv_db_id, person_id, bearbeiter_name=user_name)
            elif (schritt in _PHASE_1
                  and _phase1_komplett_erledigt(c, idv_db_id)
                  and _phase2_komplett_erledigt(c, idv_db_id)):
                # Patch-Workflow (#320) ohne Phase-2-Schritte: direkt die
                # Archivierung anstoßen, sobald Phase 1 abgeschlossen ist.
                archiv_neu  = _ensure_archiv_schritt(c, idv_db_id, person_id, bearbeiter_name=user_name)
                freigegeben = _finalisiere_freigabe_wenn_komplett(c, idv_db_id, person_id, bearbeiter_name=user_name)
        return True, freigegeben, archiv_neu

    completed, freigegeben, archiv_neu = get_writer().submit(_do, wait=True)
    if archiv_neu and not freigegeben:
        _notify_schritte(db, idv_db_id, [_PHASE_3[0]], {_PHASE_3[0]: None})
    if freigegeben:
        _notify_freigabe_erteilt(db, idv_db_id)
    return completed


# ---------------------------------------------------------------------------
# Phase 1 starten: Fachlicher Test + Technischer Test (parallel)
# ---------------------------------------------------------------------------

@bp.route("/eigenentwicklung/<int:idv_db_id>/starten", methods=["POST"])
@own_write_required
def starten(idv_db_id):
    """Startet Phase 1. Legt die in der Workflow-Konfiguration dieser IDV
    vorgesehenen Phase-1-Schritte an (grundlegend: beide Tests; patch: nur
    die explizit konfigurierten). Auch die Einstufung ``grundlegend`` /
    ``patch`` (#320) wird hier festgeschrieben – inklusive Begründung und
    GDA/DORA-Guard.
    """
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    if not _testverfahren_erforderlich(db, idv_db_id):
        row = db.execute("SELECT letzte_aenderungsart FROM idv_register WHERE id=?",
                         (idv_db_id,)).fetchone()
        if row and row["letzte_aenderungsart"] == "unwesentlich":
            flash("Kein Testverfahren erforderlich – Änderung wurde als unwesentlich eingestuft.", "info")
        else:
            flash("Freigabeverfahren nur für wesentliche Eigenentwicklungen erforderlich.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Issue #348: Freigabe erst bei vollständig gepflegter IDV.
    completeness = idv_completeness_score(db, idv_db_id)
    if completeness["score"] < 100:
        flash(
            "Freigabe-Workflow ist gesperrt: Vollständigkeits-Score {s} % – "
            "bitte zuerst diese Felder nachpflegen: {m}.".format(
                s=completeness["score"],
                m=", ".join(completeness["missing"]) or "–",
            ),
            "warning",
        )
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Guard: Phase 1 darf noch nicht gestartet sein. Prüft auf alle Phase-1-
    # Schritte (auch wenn im Patch-Workflow nur einer angelegt wird), damit
    # ein zweiter Start-Klick keine Duplikate erzeugt.
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt IN (?,?)",
        (idv_db_id, _PHASE_1[0], _PHASE_1[1])
    ).fetchone()
    if existing:
        flash("Phase 1 (Tests) wurde bereits gestartet.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Einstufung grundlegend / patch (#320)
    kategorie = (request.form.get("aenderungskategorie") or "grundlegend").strip().lower()
    if kategorie not in _KATEGORIEN:
        kategorie = "grundlegend"
    patch_begruendung = (request.form.get("patch_begruendung") or "").strip()

    if kategorie == "patch":
        # Erstfreigabe ist immer grundlegend – es gibt keinen Vorgänger,
        # an dessen Unverändertheit sich der Patch orientieren könnte.
        row_prev = db.execute(
            "SELECT vorgaenger_idv_id FROM idv_register WHERE id=?", (idv_db_id,)
        ).fetchone()
        if not row_prev or not row_prev["vorgaenger_idv_id"]:
            flash(
                "Patch-Verfahren ist bei der Erstfreigabe nicht zulässig – "
                "bitte 'grundlegend' wählen.",
                "error",
            )
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
        if not patch_begruendung:
            flash(
                "Für die Einstufung als Patch ist eine Begründung verpflichtend.",
                "error",
            )
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
        if _ist_gda4_oder_dora_kritisch(db, idv_db_id):
            flash(
                "Patch-Verfahren ist für GDA=4 / DORA-kritische IDVs gesperrt "
                "(FA-045 verlangt den vollen Workflow).",
                "error",
            )
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
        patch_schritte = set(_get_patch_schritte(db))
        phase1_schritte = [s for s in _PHASE_1 if s in patch_schritte]
    else:
        patch_begruendung = None
        phase1_schritte = list(_PHASE_1)

    zugewiesen_fachlich,  pool_id_fachlich  = _parse_combined_assignment(
        request.form.get("zugewiesen_fachlicher_test"))
    zugewiesen_technisch, pool_id_technisch = _parse_combined_assignment(
        request.form.get("zugewiesen_technischer_test"))
    zuweisungen = {
        _PHASE_1[0]: (zugewiesen_fachlich,  pool_id_fachlich),
        _PHASE_1[1]: (zugewiesen_technisch, pool_id_technisch),
    }
    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            # Kategorie + Begründung persistent pro IDV-Version speichern,
            # damit nachgelagerte Phasen und die Anzeige dieselbe Konfig
            # zu Gesicht bekommen.
            c.execute(
                "UPDATE idv_register SET freigabe_aenderungskategorie=?, "
                "freigabe_patch_begruendung=?, aktualisiert_am=? WHERE id=?",
                (kategorie, patch_begruendung, now, idv_db_id),
            )
            for schritt in phase1_schritte:
                zugewiesen, pool_id = zuweisungen[schritt]
                c.execute("""
                    INSERT INTO idv_freigaben
                        (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id, pool_id)
                    VALUES (?, ?, 'Ausstehend', ?, ?, ?, ?)
                """, (idv_db_id, schritt, person_id, now, zugewiesen, pool_id))

            _ensure_test_eintraege(c, idv_db_id)

            c.execute(
                "UPDATE idv_register SET teststatus='Freigabe ausstehend', aktualisiert_am=? WHERE id=?",
                (now, idv_db_id),
            )
            if kategorie == "patch":
                entfallen = [s for s in _SCHRITTE if s not in set(_get_patch_schritte(c))]
                hist_kom = (
                    "Freigabeverfahren gestartet – Einstufung: PATCH "
                    f"(verkürzter Workflow). Aktive Schritte: {', '.join(_get_patch_schritte(c))}. "
                    f"Entfallen: {', '.join(entfallen) or 'keine'}. "
                    f"Begründung: {patch_begruendung}"
                )
                hist_aktion = "freigabe_gestartet_patch"
            else:
                hist_kom = ("Freigabeverfahren gestartet – Einstufung: GRUNDLEGEND "
                            "(voller 3-Phasen-Workflow).")
                hist_aktion = "freigabe_gestartet"
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, hist_aktion, hist_kom, person_id, user_name),
            )

    get_writer().submit(_do, wait=True)

    _notify_schritte(
        db, idv_db_id, phase1_schritte,
        {s: zuweisungen[s][0] for s in phase1_schritte},
        {s: zuweisungen[s][1] for s in phase1_schritte},
    )

    if kategorie == "patch":
        if not phase1_schritte:
            # Patch ohne Phase-1-Schritt: Phase 2 direkt freigeben (und ggf.
            # die automatische Archivierung anstoßen), damit der Anwender
            # ohne Umweg weiter kommt.
            flash(
                "Patch-Verfahren gestartet – Phase 1 entfällt laut Konfiguration. "
                "Bitte Phase 2 (Abnahmen) starten.",
                "success",
            )
        else:
            flash(
                f"Patch-Verfahren gestartet: {', '.join(phase1_schritte)}.",
                "success",
            )
    else:
        flash("Phase 1 gestartet: Fachlicher Test und Technischer Test laufen parallel.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Phase 2 starten: Fachliche Abnahme + Technische Abnahme (parallel)
# ---------------------------------------------------------------------------

@bp.route("/eigenentwicklung/<int:idv_db_id>/abnahme-starten", methods=["POST"])
@own_write_required
def abnahme_starten(idv_db_id):
    """Startet Phase 2. Legt die in der Workflow-Konfiguration dieser IDV
    vorgesehenen Phase-2-Schritte an (grundlegend: beide Abnahmen; patch:
    nur die konfigurierten)."""
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    if not _phase1_komplett_erledigt(db, idv_db_id):
        flash("Phase 2 kann erst gestartet werden, wenn Phase 1 vollständig erledigt ist.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Guard: Phase 2 darf noch nicht gestartet sein
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt IN (?,?)",
        (idv_db_id, _PHASE_2[0], _PHASE_2[1])
    ).fetchone()
    if existing:
        flash("Phase 2 (Abnahmen) wurde bereits gestartet.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    _, phase2_schritte, _ = _active_phase_schritte(db, idv_db_id)
    if not phase2_schritte:
        flash(
            "Für diese IDV sind laut Patch-Konfiguration keine Abnahmen vorgesehen.",
            "info",
        )
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    zugewiesen_fachlich,  pool_id_fachlich  = _parse_combined_assignment(
        request.form.get("zugewiesen_fachliche_abnahme"))
    zugewiesen_technisch, pool_id_technisch = _parse_combined_assignment(
        request.form.get("zugewiesen_technische_abnahme"))
    zuweisungen = {
        _PHASE_2[0]: (zugewiesen_fachlich,  pool_id_fachlich),
        _PHASE_2[1]: (zugewiesen_technisch, pool_id_technisch),
    }
    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            for schritt in phase2_schritte:
                zugewiesen, pool_id = zuweisungen[schritt]
                c.execute("""
                    INSERT INTO idv_freigaben
                        (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id, pool_id)
                    VALUES (?, ?, 'Ausstehend', ?, ?, ?, ?)
                """, (idv_db_id, schritt, person_id, now, zugewiesen, pool_id))

            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_phase2_gestartet",
                 f"Phase 2 gestartet: {', '.join(phase2_schritte)}",
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)

    _notify_schritte(
        db, idv_db_id, phase2_schritte,
        {s: zuweisungen[s][0] for s in phase2_schritte},
        {s: zuweisungen[s][1] for s in phase2_schritte},
    )

    flash(f"Phase 2 gestartet: {', '.join(phase2_schritte)}.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Vollseiten-Formular: Schritt als Erledigt markieren (GET)
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/erledigt", methods=["GET"])
@own_write_required
def erledigt_seite(freigabe_id):
    """Zeigt das Formular zum Abschließen eines Freigabe-Schritts (oder read-only wenn bereits abgeschlossen)."""
    db = get_db()
    freigabe = db.execute("""
        SELECT f.*,
               p_d.nachname || ', ' || p_d.vorname AS durchgefuehrt_von,
               p_z.nachname || ', ' || p_z.vorname AS zugewiesen_an,
               pool.name AS pool_name
        FROM idv_freigaben f
        LEFT JOIN persons p_d ON f.durchgefuehrt_von_id = p_d.id
        LEFT JOIN persons p_z ON f.zugewiesen_an_id     = p_z.id
        LEFT JOIN freigabe_pools pool ON f.pool_id      = pool.id
        WHERE f.id = ?
    """, (freigabe_id,)).fetchone()
    if not freigabe:
        flash("Freigabe-Schritt nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))
    ensure_can_read_idv(db, freigabe["idv_id"])
    idv = db.execute("SELECT * FROM idv_register WHERE id=?", (freigabe["idv_id"],)).fetchone()
    if not idv:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    # Phase-1-Schritte: immer zur spezialisierten Testmaske weiterleiten
    if freigabe["schritt"] == "Fachlicher Test":
        kwargs = {"idv_db_id": idv["id"]}
        if freigabe["status"] == "Ausstehend":
            kwargs["freigabe_id"] = freigabe_id
        return redirect(url_for("tests.new_fachlicher_testfall", **kwargs))
    if freigabe["schritt"] == "Technischer Test":
        kwargs = {"idv_db_id": idv["id"]}
        if freigabe["status"] == "Ausstehend":
            kwargs["freigabe_id"] = freigabe_id
        return redirect(url_for("tests.edit_technischer_test", **kwargs))

    # Stellvertreter-Info: aktiver Vertreter der zugewiesenen Person
    vertreter_name = None
    if freigabe["zugewiesen_an_id"] and freigabe["status"] == "Ausstehend":
        stv_id = _get_aktiver_stellvertreter_id(db, freigabe["zugewiesen_an_id"])
        if stv_id:
            stv = db.execute(
                "SELECT nachname || ', ' || vorname AS name FROM persons WHERE id=?",
                (stv_id,)
            ).fetchone()
            if stv:
                vertreter_name = stv["name"]

    persons = db.execute(
        "SELECT id, nachname || ', ' || vorname AS name FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    try:
        freigabe_pools = db.execute(
            "SELECT id, name FROM freigabe_pools WHERE aktiv=1 ORDER BY name"
        ).fetchall()
    except Exception:
        freigabe_pools = []

    # Read-only auch dann, wenn der Benutzer den Schritt zwar lesen, aber
    # nicht abschließen darf (Funktionstrennung oder fehlende Zuweisung).
    # Verhindert, dass das Abschluss-Formular überhaupt sichtbar wird,
    # obwohl der POST ohnehin durch _funktionstrennung_ok / _can_complete_schritt
    # abgelehnt würde.
    # Admins dürfen auch ohne persons-Eintrag (z.B. lokaler config.json-User
    # ohne person_id) — die beiden Helfer haben intern Admin-Bypasses, aber der
    # ``pid is not None``-Gate feuerte vorher zu früh.
    from . import ROLE_ADMIN as _ROLE_ADMIN
    pid = current_person_id()
    if session.get("user_role") == _ROLE_ADMIN:
        darf_abschliessen = True
    else:
        darf_abschliessen = (
            pid is not None
            and _funktionstrennung_ok(db, freigabe["idv_id"], pid)
            and _can_complete_schritt(db, freigabe, pid)
        )

    # Phase 3: Archivierungs-Schritt → spezialisierte Maske
    if freigabe["schritt"] in _PHASE_3:
        readonly = freigabe["status"] != "Ausstehend" or not darf_abschliessen
        scanner_dateien = _verfuegbare_scanner_dateien(db, idv["id"]) if not readonly else []
        return render_template("freigaben/archiv_form.html",
                               freigabe=freigabe, idv=idv, readonly=readonly,
                               scanner_dateien=scanner_dateien,
                               vertreter_name=vertreter_name, persons=persons,
                               freigabe_pools=freigabe_pools)

    # Phase 2: Abnahmeformular – bearbeitbar wenn Ausstehend und zuständig, sonst Lesemodus
    readonly = freigabe["status"] != "Ausstehend" or not darf_abschliessen
    scanner_dateien = _verfuegbare_scanner_dateien(db, idv["id"]) if not readonly else []

    # Für die Fachliche Abnahme: Excel-Dateien ohne Zell-/Blattschutz anzeigen.
    # Auch im Lesemodus, damit dokumentiert ist, wer welche Ausnahme akzeptiert hat.
    ungeschuetzte_excel = (
        _unprotected_excel_files_for_idv(db, idv["id"])
        if freigabe["schritt"] == "Fachliche Abnahme" else []
    )

    return render_template("freigaben/bestanden_form.html",
                           freigabe=freigabe, idv=idv, readonly=readonly,
                           vertreter_name=vertreter_name, persons=persons,
                           scanner_dateien=scanner_dateien,
                           ungeschuetzte_excel=ungeschuetzte_excel,
                           freigabe_pools=freigabe_pools)


# ---------------------------------------------------------------------------
# Schritt abschließen
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/abschliessen", methods=["POST"])
@own_write_required
def abschliessen(freigabe_id):
    """Schließt einen Freigabe-Schritt als 'Erledigt' ab."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        if is_xhr:
            return jsonify({"ok": False, "error": "Freigabe-Schritt nicht gefunden oder bereits abgeschlossen."}), 400
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        err = ("Funktionstrennung: Sie sind als Entwickler dieser Eigenentwicklung eingetragen "
               "und dürfen keine Freigabe-Schritte abschließen.")
        if is_xhr:
            return jsonify({"ok": False, "error": err}), 403
        flash(err, "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    if not _can_complete_schritt(db, freigabe, person_id):
        err = "Nur die zugewiesene Person oder deren aktiver Stellvertreter darf diesen Schritt abschließen."
        if is_xhr:
            return jsonify({"ok": False, "error": err}), 403
        flash(err, "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    kommentar = request.form.get("kommentar", "").strip() or None
    # VULN-C: Quill-Rich-Text vor dem Speichern entschärfen (bleach).
    nachweise = sanitize_html(request.form.get("nachweise_text", ""))

    nachweis_pfad = nachweis_name = None
    upload_file = request.files.get("nachweis_datei")
    if upload_file and upload_file.filename:
        saved, orig = _save_upload(upload_file)
        if saved:
            nachweis_pfad, nachweis_name = saved, orig
        else:
            if is_xhr:
                return jsonify({"ok": False, "error": "Ungültiges Dateiformat für Nachweis-Upload."}), 400
            flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
    elif request.form.get("scanner_file_id"):
        sf_id = _int_or_none(request.form.get("scanner_file_id"))
        if sf_id:
            sf = db.execute("SELECT file_name FROM idv_files WHERE id=?", (sf_id,)).fetchone()
            if sf:
                nachweis_pfad = f"scanner:{sf_id}"
                nachweis_name = sf["file_name"]

    schritt = freigabe["schritt"]

    # Fachliche Abnahme: Wenn verknüpfte Excel-Dateien ohne Zell-/Blattschutz
    # existieren, muss der Fachverantwortliche jede dieser Dateien bewusst
    # akzeptieren (MaRisk AT 7.2 / DORA). Begründung ist optional.
    zellschutz_akzeptanzen: list[tuple[int, str | None]] = []
    if schritt == "Fachliche Abnahme":
        ungeschuetzt = _unprotected_excel_files_for_idv(db, idv_db_id)
        fehlend: list[str] = []
        for datei in ungeschuetzt:
            if datei.get("akzeptiert_am"):
                continue  # bereits früher akzeptiert → nicht erneut einfordern
            akz_flag = request.form.get(f"zellschutz_akz_{datei['id']}")
            if akz_flag != "1":
                fehlend.append(datei["file_name"])
                continue
            begr = (request.form.get(f"zellschutz_begr_{datei['id']}") or "").strip() or None
            zellschutz_akzeptanzen.append((datei["id"], begr))
        if fehlend:
            err = (
                "Fehlender Zell-/Blattschutz muss bewusst akzeptiert werden: "
                + ", ".join(fehlend)
            )
            if is_xhr:
                return jsonify({"ok": False, "error": err}), 400
            flash(err, "error")
            return redirect(url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id))

    user_name = session.get("user_name", "") or None
    hist_aktion, hist_kommentar = _sod_log_fields(
        db, idv_db_id, person_id,
        "freigabe_schritt_erledigt", f"{schritt} erledigt",
    )

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE idv_freigaben
                SET status='Erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
                    kommentar=?, nachweise_text=?, nachweis_datei_pfad=?, nachweis_datei_name=?
                WHERE id=?
            """, (person_id, now, kommentar, nachweise, nachweis_pfad, nachweis_name, freigabe_id))

            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, hist_aktion, hist_kommentar, person_id, user_name),
            )

            for file_id, begr in zellschutz_akzeptanzen:
                c.execute("""
                    INSERT INTO idv_zellschutz_akzeptanz
                        (idv_db_id, file_id, freigabe_id, akzeptiert_von_id,
                         akzeptiert_am, begruendung)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (idv_db_id, file_id, freigabe_id, person_id, now, begr))

            phase2_done = False
            phase1_done = False
            freigegeben = False
            archiv_neu  = False
            if schritt in _PHASE_2 and _phase2_komplett_erledigt(c, idv_db_id):
                phase2_done = True
                archiv_neu  = _ensure_archiv_schritt(c, idv_db_id, person_id, bearbeiter_name=user_name)
                freigegeben = _finalisiere_freigabe_wenn_komplett(c, idv_db_id, person_id, bearbeiter_name=user_name)
            elif schritt in _PHASE_1 and _phase1_komplett_erledigt(c, idv_db_id):
                phase1_done = True
                # Patch-Workflow ohne Phase-2-Schritte: Phase 1 geht direkt
                # in die Archivierung über, damit kein toter Button im UI
                # erscheint ("Phase 2 starten" mit leerem Inhalt).
                if _phase2_komplett_erledigt(c, idv_db_id):
                    archiv_neu  = _ensure_archiv_schritt(c, idv_db_id, person_id, bearbeiter_name=user_name)
                    freigegeben = _finalisiere_freigabe_wenn_komplett(c, idv_db_id, person_id, bearbeiter_name=user_name)
        return phase2_done, phase1_done, freigegeben, archiv_neu

    phase2_done, phase1_done, freigegeben, archiv_neu = get_writer().submit(_do, wait=True)

    if archiv_neu and not freigegeben:
        _notify_schritte(db, idv_db_id, [_PHASE_3[0]], {_PHASE_3[0]: None})

    detail_url = url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id)

    if freigegeben:
        _notify_freigabe_erteilt(db, idv_db_id)
        msg = "Alle Freigabe-Schritte erledigt – Eigenentwicklung ist jetzt freigegeben."
        cat = "phase_transition"
        redirect_url = detail_url + "#freigabeverfahren"
    elif phase2_done:
        if archiv_neu:
            msg = (f"'{schritt}' erledigt – Phase 2 vollständig. "
                   "Die Archivierung wurde automatisch angelegt (Phase 3).")
            cat = "phase_transition"
            redirect_url = detail_url + "#archivierung"
        else:
            msg = f"'{schritt}' erledigt – Phase 2 vollständig."
            cat = "phase_transition"
            redirect_url = detail_url + "#freigabeverfahren"
    elif phase1_done:
        msg = f"'{schritt}' erledigt – Phase 1 vollständig. Phase 2 (Abnahmen) ist nun startbereit."
        cat = "phase_transition"
        redirect_url = detail_url + "#freigabeverfahren"
    else:
        msg = f"'{schritt}' als Erledigt markiert."
        cat = "success"
        redirect_url = detail_url + "#freigabeverfahren"

    if is_xhr:
        return jsonify({"ok": True, "msg": msg, "cat": cat, "redirect_url": redirect_url})
    flash(msg, cat)
    return redirect(redirect_url)


# ---------------------------------------------------------------------------
# Schritt ablehnen
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/ablehnen", methods=["POST"])
@own_write_required
def ablehnen(freigabe_id):
    """Markiert einen Freigabe-Schritt als 'Nicht erledigt'."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()
    is_xhr    = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        if is_xhr:
            return jsonify({"ok": False, "error": "Freigabe-Schritt nicht gefunden oder bereits abgeschlossen."}), 400
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        err = "Funktionstrennung: Sie sind als Entwickler eingetragen und dürfen keine Freigabe-Schritte ablehnen."
        if is_xhr:
            return jsonify({"ok": False, "error": err}), 403
        flash(err, "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    if not _can_complete_schritt(db, freigabe, person_id):
        err = "Nur die zugewiesene Person oder deren aktiver Stellvertreter darf diesen Schritt ablehnen."
        if is_xhr:
            return jsonify({"ok": False, "error": err}), 403
        flash(err, "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    befunde   = request.form.get("befunde", "").strip() or None
    kommentar = request.form.get("kommentar", "").strip() or None
    # VULN-C: Quill-Rich-Text vor dem Speichern entschärfen (bleach).
    nachweise = sanitize_html(request.form.get("nachweise_text", ""))

    nachweis_pfad = nachweis_name = None
    upload_file = request.files.get("nachweis_datei")
    if upload_file and upload_file.filename:
        saved, orig = _save_upload(upload_file)
        if saved:
            nachweis_pfad, nachweis_name = saved, orig

    schritt_name = freigabe["schritt"]
    user_name = session.get("user_name", "") or None
    hist_aktion, hist_kommentar = _sod_log_fields(
        db, idv_db_id, person_id,
        "freigabe_abgelehnt",
        f"{schritt_name} nicht erledigt. Befunde: {befunde}",
    )

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE idv_freigaben
                SET status='Nicht erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
                    befunde=?, kommentar=?, nachweise_text=?,
                    nachweis_datei_pfad=?, nachweis_datei_name=?
                WHERE id=?
            """, (person_id, now, befunde, kommentar, nachweise,
                  nachweis_pfad, nachweis_name, freigabe_id))

            c.execute(
                "UPDATE idv_register SET teststatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
                (now, idv_db_id),
            )
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, hist_aktion, hist_kommentar, person_id, user_name),
            )

    get_writer().submit(_do, wait=True)

    redirect_url = url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id) + "#freigabeverfahren"
    if is_xhr:
        return jsonify({"ok": True, "redirect_url": redirect_url})
    flash(f"'{schritt_name}' nicht erledigt.", "warning")
    return redirect(redirect_url)


# ---------------------------------------------------------------------------
# Admin: Verfahren abbrechen
# ---------------------------------------------------------------------------

@bp.route("/eigenentwicklung/<int:idv_db_id>/abbrechen", methods=["POST"])
@admin_required
def abbrechen(idv_db_id):
    """Admin bricht das laufende Freigabeverfahren ab."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    kommentar = request.form.get("abbruch_kommentar", "").strip() or None

    ph, ph_params = in_clause(_PHASE_3)
    offene = db.execute(
        f"SELECT id FROM idv_freigaben WHERE idv_id=? AND status='Ausstehend' AND schritt NOT IN ({ph})",
        [idv_db_id] + list(ph_params)
    ).fetchall()

    if not offene:
        flash("Kein laufendes Freigabeverfahren gefunden.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    offene_ids = [row["id"] for row in offene]
    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            for fid in offene_ids:
                c.execute("""
                    UPDATE idv_freigaben
                    SET status='Abgebrochen', abgebrochen_von_id=?, abgebrochen_am=?, abbruch_kommentar=?
                    WHERE id=?
                """, (person_id, now, kommentar, fid))

            c.execute(
                "UPDATE idv_register SET teststatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
                (now, idv_db_id),
            )
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_abgebrochen",
                 "Freigabeverfahren durch Administrator abgebrochen."
                 + (f" Grund: {kommentar}" if kommentar else ""),
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)

    flash("Freigabeverfahren wurde abgebrochen.", "warning")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Einzelnen Freigabe-Schritt wieder anlegen (nach Löschung)
# ---------------------------------------------------------------------------

@bp.route("/eigenentwicklung/<int:idv_db_id>/schritt-anlegen", methods=["POST"])
@own_write_required
def schritt_anlegen(idv_db_id):
    """Legt einen einzelnen Freigabe-Schritt wieder an, wenn er zuvor
    gelöscht wurde. Funktioniert für Phase-1- und Phase-2-Schritte."""
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()
    schritt   = (request.form.get("schritt") or "").strip()

    if schritt not in _SCHRITTE:
        flash("Unbekannter Freigabe-Schritt.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Für Phase 2 ist erforderlich, dass Phase 1 komplett erledigt ist
    if schritt in _PHASE_2 and not _phase1_komplett_erledigt(db, idv_db_id):
        flash("Phase-2-Schritte können erst nach kompletter Phase 1 angelegt werden.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # Phase 3 (Archivierung) kann jederzeit angelegt werden

    # Duplikats-Guard: Schritt darf nicht bereits existieren
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt=? LIMIT 1",
        (idv_db_id, schritt)
    ).fetchone()
    if existing:
        flash(f"'{schritt}' existiert bereits.", "info")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    zugewiesen, pool_id = _parse_combined_assignment(request.form.get("zugewiesen_an_id"))
    # Phase 3 (Archivierung): keine vorgelagerte Zuweisung nötig — jede
    # schreibberechtigte Person darf die Archivierung durchführen
    # (Funktionstrennung bleibt per `_funktionstrennung_ok` erzwungen).
    if schritt not in _PHASE_3 and not zugewiesen and not pool_id:
        flash("Bitte eine Person oder einen Pool für den Schritt auswählen.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO idv_freigaben
                    (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id, pool_id)
                VALUES (?, ?, 'Ausstehend', ?, ?, ?, ?)
            """, (idv_db_id, schritt, person_id, now, zugewiesen, pool_id))

            if schritt in _PHASE_1:
                _ensure_test_eintraege(c, idv_db_id)

            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_schritt_angelegt", f"{schritt} erneut angelegt", person_id, user_name),
            )

    get_writer().submit(_do, wait=True)

    _notify_schritte(db, idv_db_id, [schritt], {schritt: zugewiesen},
                     {schritt: pool_id})

    # Phase 3: direkt zur Archivierungs-Maske weiterleiten, damit der Nutzer
    # die Datei in einem Rutsch archivieren kann — ohne zweiten Klick.
    if schritt in _PHASE_3:
        neue_fr = db.execute(
            "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt=? "
            "ORDER BY id DESC LIMIT 1",
            (idv_db_id, schritt),
        ).fetchone()
        if neue_fr:
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=neue_fr["id"]))

    flash(f"'{schritt}' wurde angelegt.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Freigabe-Schritt löschen (Admin)
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/loeschen", methods=["POST"])
@admin_required
def loeschen(freigabe_id):
    """Admin löscht einen einzelnen Freigabe-Schritt."""
    db        = get_db()
    person_id = current_person_id()
    freigabe  = db.execute("SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)).fetchone()
    if not freigabe:
        flash("Freigabe-Schritt nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))
    idv_db_id = freigabe["idv_id"]
    schritt   = freigabe["schritt"]
    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM idv_freigaben WHERE id=?", (freigabe_id,))
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_schritt_geloescht", f"{schritt} gelöscht", person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    flash(f"'{schritt}' wurde gelöscht.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Abnahme-Schritt nachträglich wieder öffnen (Admin)
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/wieder-oeffnen", methods=["POST"])
@admin_required
def wieder_oeffnen(freigabe_id):
    """Admin öffnet einen abgeschlossenen Abnahme-Schritt erneut zur Beantwortung.

    Nur für Phase 2 (Abnahmen). Voraussetzung: IDV ist nicht als 'Freigegeben'
    markiert und die Archivierung (Phase 3) ist noch nicht erledigt –
    andernfalls muss der Admin den Zustand zunächst auf anderem Weg zurückbauen.
    """
    db        = get_db()
    person_id = current_person_id()
    freigabe  = db.execute("SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)).fetchone()
    if not freigabe:
        flash("Freigabe-Schritt nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    idv_db_id = freigabe["idv_id"]
    schritt   = freigabe["schritt"]

    if schritt not in _PHASE_2:
        flash("Wieder-Öffnen ist nur für Abnahmen (Phase 2) vorgesehen.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    if freigabe["status"] == "Ausstehend":
        flash(f"'{schritt}' ist bereits offen.", "info")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    idv = db.execute(
        "SELECT teststatus FROM idv_register WHERE id=?", (idv_db_id,)
    ).fetchone()
    if idv and idv["teststatus"] == "Freigegeben":
        flash("IDV ist bereits freigegeben – bitte zuerst die Freigabe rückgängig machen.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    archiv_erledigt = db.execute(
        "SELECT 1 FROM idv_freigaben WHERE idv_id=? AND schritt=? AND status='Erledigt' LIMIT 1",
        (idv_db_id, _PHASE_3[0])
    ).fetchone()
    if archiv_erledigt:
        flash("Archivierung ist bereits erledigt – Abnahme kann nicht mehr geöffnet werden.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE idv_freigaben
                SET status='Ausstehend',
                    durchgefuehrt_von_id=NULL, durchgefuehrt_am=NULL,
                    kommentar=NULL, nachweise_text=NULL,
                    nachweis_datei_pfad=NULL, nachweis_datei_name=NULL,
                    befunde=NULL
                WHERE id=?
            """, (freigabe_id,))
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_schritt_wiedereroeffnet",
                 f"{schritt} durch Admin zur erneuten Beantwortung geöffnet",
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    _notify_schritte(db, idv_db_id, [schritt], {schritt: freigabe["zugewiesen_an_id"]},
                     {schritt: freigabe["pool_id"]})
    flash(f"'{schritt}' wurde wieder geöffnet und kann erneut beantwortet werden.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Freigabe-Schritt weiterleiten (an Dritte delegieren)
# ---------------------------------------------------------------------------


@bp.route("/<int:freigabe_id>/uebernehmen", methods=["POST"])
@login_required
def uebernehmen(freigabe_id):
    """Pool-Mitglied übernimmt eine Pool-Aufgabe persönlich (U-D4).

    Setzt zugewiesen_an_id auf den Aufrufer und hebt die Pool-Bindung auf.
    Nur Mitglieder des zugewiesenen Pools (oder Admins) dürfen übernehmen.
    """
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id = ?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    pool_id = None
    try:
        pool_id = freigabe["pool_id"]
    except (KeyError, IndexError):
        pool_id = None
    if not pool_id:
        flash("Dieser Schritt ist keinem Pool zugewiesen.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=freigabe["idv_id"]))

    from . import ROLE_ADMIN
    is_admin = session.get("user_role") == ROLE_ADMIN
    if not is_admin and not _is_pool_member(db, pool_id, person_id):
        flash("Sie sind kein Mitglied des zugewiesenen Pools.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=freigabe["idv_id"]))

    if not person_id:
        flash("Kein Personendatensatz für diesen Account.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=freigabe["idv_id"]))

    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_freigaben SET zugewiesen_an_id = ?, pool_id = NULL WHERE id = ?",
                (person_id, freigabe_id),
            )
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (freigabe["idv_id"], "freigabe_uebernommen",
                 f"{freigabe['schritt']} aus Pool übernommen", person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    flash(f"'{freigabe['schritt']}' übernommen – die Aufgabe ist jetzt Ihnen zugewiesen.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=freigabe["idv_id"]))


# ---------------------------------------------------------------------------
# Soft-Claim auf Pool-Schritt (#321) — reversibel
# ---------------------------------------------------------------------------


@bp.route("/<int:freigabe_id>/claim", methods=["POST"])
@login_required
def claim(freigabe_id):
    """Soft-Claim auf einen Pool-Schritt: markiert die Aufgabe als
    „in Bearbeitung durch <Person>", ohne die Pool-Bindung aufzuheben.
    Andere Pool-Mitglieder sehen den Claim-Inhaber und erhalten keine
    Erinnerungen mehr. Der Claim kann über ``claim-loesen`` zurückgegeben
    werden. Abschluss bleibt für alle Pool-Mitglieder möglich.
    """
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id = ?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    pool_id = None
    try:
        pool_id = freigabe["pool_id"]
    except (KeyError, IndexError):
        pool_id = None
    if not pool_id:
        flash("Dieser Schritt ist keinem Pool zugewiesen.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))

    from . import ROLE_ADMIN
    is_admin = session.get("user_role") == ROLE_ADMIN
    if not is_admin and not _is_pool_member(db, pool_id, person_id):
        flash("Sie sind kein Mitglied des zugewiesenen Pools.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))
    if not person_id:
        flash("Kein Personendatensatz für diesen Account.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))

    # Ist bereits ein anderer Claim aktiv? Nur der Inhaber oder Admin darf
    # ihn überschreiben. Reiner „Refresh-Klick" des Inhabers ist OK.
    try:
        current = freigabe["bearbeitet_von_id"]
    except (KeyError, IndexError):
        current = None
    if current and current != person_id and not is_admin:
        flash("Diese Aufgabe wird bereits bearbeitet. "
              "Bitte mit dem aktuellen Bearbeiter abstimmen.", "info")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))

    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_freigaben "
                "SET bearbeitet_von_id=?, bearbeitet_am=? WHERE id=?",
                (person_id, now, freigabe_id),
            )
            c.execute(
                "INSERT INTO idv_history "
                "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                "VALUES (?,?,?,?,?)",
                (freigabe["idv_id"], "freigabe_claim",
                 f"{freigabe['schritt']} zur Bearbeitung übernommen",
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    flash(f"'{freigabe['schritt']}' zur Bearbeitung übernommen.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv",
                            idv_db_id=freigabe["idv_id"]))


@bp.route("/<int:freigabe_id>/claim-loesen", methods=["POST"])
@login_required
def claim_loesen(freigabe_id):
    """Gibt einen Soft-Claim zurück — andere Pool-Mitglieder können die
    Aufgabe dann wieder übernehmen. Nur der aktuelle Claim-Inhaber oder
    ein Admin dürfen lösen.
    """
    db        = get_db()
    person_id = current_person_id()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id = ?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    try:
        current = freigabe["bearbeitet_von_id"]
    except (KeyError, IndexError):
        current = None
    if not current:
        flash("Auf diesen Schritt besteht kein Claim.", "info")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))

    from . import ROLE_ADMIN
    is_admin = session.get("user_role") == ROLE_ADMIN
    if current != person_id and not is_admin:
        flash("Nur der aktuelle Bearbeiter oder ein Admin darf den Claim lösen.",
              "error")
        return redirect(url_for("eigenentwicklung.detail_idv",
                                idv_db_id=freigabe["idv_id"]))

    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_freigaben "
                "SET bearbeitet_von_id=NULL, bearbeitet_am=NULL WHERE id=?",
                (freigabe_id,),
            )
            c.execute(
                "INSERT INTO idv_history "
                "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                "VALUES (?,?,?,?,?)",
                (freigabe["idv_id"], "freigabe_claim_geloest",
                 f"{freigabe['schritt']} wieder freigegeben (Claim gelöst)",
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    flash(f"'{freigabe['schritt']}' wieder freigegeben — Pool kann erneut übernehmen.",
          "success")
    return redirect(url_for("eigenentwicklung.detail_idv",
                            idv_db_id=freigabe["idv_id"]))


# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/weiterleiten", methods=["POST"])
@own_write_required
def weiterleiten(freigabe_id):
    """Leitet einen ausstehenden Freigabe-Schritt an eine andere Person weiter.

    Ermöglicht die Delegation an einen Dritten (MaRisk AT 7.2 Stellvertreter-
    Regelung). Jeder Schreibberechtigte der IDV darf weiterleiten; ein
    History-Eintrag und eine E-Mail-Benachrichtigung werden erzeugt.
    """
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    neuer_id, pool_id = _parse_combined_assignment(request.form.get("weiterleiten_an_id"))
    if not neuer_id and not pool_id:
        flash("Bitte eine Person oder einen Pool für die Weiterleitung auswählen.", "error")
        return redirect(url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id))

    schritt   = freigabe["schritt"]
    user_name = session.get("user_name", "") or None

    if pool_id:
        pool = db.execute(
            "SELECT name FROM freigabe_pools WHERE id=? AND aktiv=1", (pool_id,)
        ).fetchone()
        if not pool:
            flash("Ausgewählter Pool nicht gefunden.", "error")
            return redirect(url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id))

        ziel_name = f"Pool: {pool['name']}"

        def _do(c):
            with write_tx(c):
                c.execute(
                    "UPDATE idv_freigaben SET zugewiesen_an_id=NULL, pool_id=? WHERE id=?",
                    (pool_id, freigabe_id)
                )
                c.execute(
                    "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                    (idv_db_id, "freigabe_weitergeleitet",
                     f"{schritt} weitergeleitet an {ziel_name}",
                     person_id, user_name),
                )

        get_writer().submit(_do, wait=True)
        _notify_schritte(db, idv_db_id, [schritt], {schritt: None},
                         {schritt: pool_id})
        flash(f"'{schritt}' wurde an {ziel_name} weitergeleitet.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    p = db.execute(
        "SELECT nachname, vorname FROM persons WHERE id=? AND aktiv=1", (neuer_id,)
    ).fetchone()
    if not p:
        flash("Ausgewählte Person nicht gefunden.", "error")
        return redirect(url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id))

    p_name    = f"{p['nachname']}, {p['vorname']}"

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_freigaben SET zugewiesen_an_id=?, pool_id=NULL WHERE id=?",
                (neuer_id, freigabe_id)
            )
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "freigabe_weitergeleitet",
                 f"{schritt} weitergeleitet an {p_name}",
                 person_id, user_name),
            )

    get_writer().submit(_do, wait=True)
    _notify_schritte(db, idv_db_id, [schritt], {schritt: neuer_id})
    flash(f"'{schritt}' wurde an {p_name} weitergeleitet.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Phase 3: Archivierung der Originaldatei (revisionssicher, MaRisk AT 7.2)
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/archivieren", methods=["POST"])
@own_write_required
def archivieren(freigabe_id):
    """Schließt den Archivierungs-Schritt (Phase 3) ab.

    Drei Abschlusspfade über das Formularfeld ``archiv_quelle``:
    - ``upload`` (Standard): Originaldatei wird hochgeladen, im Archiv
      schreibgeschützt abgelegt und mit SHA-256-Hash versehen.
    - ``scanner``: Eine bereits vom Scanner gefundene Datei (verknüpft
      über ``idv_register.file_id`` oder ``idv_file_links``) wird vom
      Quellpfad in das Archiv kopiert; SHA-256 wird neu berechnet.
    - ``nicht_verfuegbar``: Die Datei selbst ist nicht verfügbar
      (z.B. Cognos-/agree21Analysen-Berichte). Begründung ist Pflicht;
      der Statusschritt wird trotzdem revisionssicher festgehalten.
    """
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if (not freigabe
            or freigabe["schritt"] not in _PHASE_3
            or freigabe["status"] != "Ausstehend"):
        flash("Archivierungs-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser Eigenentwicklung eingetragen "
            "und dürfen die Archivierung nicht abschließen.", "error"
        )
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    if not _can_complete_schritt(db, freigabe, person_id):
        flash(
            "Nur die zugewiesene Person oder deren aktiver Stellvertreter "
            "darf die Archivierung abschließen.", "error"
        )
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    quelle = (request.form.get("archiv_quelle") or "upload").strip().lower()
    if quelle not in ("upload", "scanner", "nicht_verfuegbar"):
        quelle = "upload"

    kommentar    = request.form.get("kommentar", "").strip() or None
    begruendung  = request.form.get("archiv_begruendung", "").strip() or None

    archiv_pfad = archiv_name = archiv_sha256 = None
    befunde          = None
    datei_verfuegbar = 1 if quelle in ("upload", "scanner") else 0

    if quelle == "upload":
        upload_file = request.files.get("archiv_datei")
        if not upload_file or not upload_file.filename:
            flash(
                "Bitte die Originaldatei zum Archivieren hochladen oder "
                "eine andere Quelle auswählen.", "error"
            )
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        original_name = upload_file.filename
        # Für das Archiv werden KEINE Extension- oder Magic-Byte-Prüfungen
        # vorgenommen, weil die Originaldatei in beliebigen Formaten
        # (VBA-Makro, Python-Skript, SQL-Datei, Access-DB, PBIX, …) vorliegen
        # kann. Schutz erfolgt stattdessen durch sicheren Dateinamen,
        # getrenntes Zielverzeichnis und read-only-Ablage.
        safe_name = secure_filename(original_name) or "original.bin"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
        save_name = timestamp + safe_name
        folder = _archiv_upload_folder(idv_db_id)
        dest = os.path.join(folder, save_name)

        # Streamed-Speichern + SHA-256-Berechnung (Revisionssicherheit),
        # mit harter Obergrenze zum Schutz gegen DoS / Disk-Full.
        h = hashlib.sha256()
        total = 0
        try:
            upload_file.stream.seek(0)
        except Exception:
            pass
        try:
            with open(dest, "wb") as out:
                while True:
                    chunk = upload_file.stream.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ARCHIV_UPLOAD:
                        out.close()
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                        flash(
                            "Archiv-Upload abgelehnt: Datei ist größer als "
                            f"{_MAX_ARCHIV_UPLOAD // (1024 * 1024)} MB.",
                            "error",
                        )
                        return redirect(url_for("freigaben.erledigt_seite",
                                                freigabe_id=freigabe_id))
                    out.write(chunk)
                    h.update(chunk)
        except OSError as exc:
            current_app.logger.warning(
                "Archiv-Upload fehlgeschlagen für IDV %s: %s", idv_db_id, exc
            )
            flash("Archiv-Upload fehlgeschlagen (Dateisystem-Fehler).", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        archiv_sha256 = h.hexdigest()
        archiv_pfad   = save_name
        archiv_name   = original_name

        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass

        befunde = (
            begruendung
            or f"Originaldatei (Upload) archiviert (SHA-256: {archiv_sha256})"
        )

    elif quelle == "scanner":
        try:
            scanner_file_id = int(request.form.get("scanner_file_id") or 0)
        except (TypeError, ValueError):
            scanner_file_id = 0
        if not scanner_file_id:
            flash("Bitte eine Scanner-Datei zur Übernahme auswählen.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        # Sicherstellen, dass die Datei tatsächlich mit dieser IDV verknüpft
        # ist – sonst dürfte ein Nutzer beliebige Scanner-Funde kopieren.
        verfuegbar = {f["id"] for f in _verfuegbare_scanner_dateien(db, idv_db_id)}
        if scanner_file_id not in verfuegbar:
            flash("Die ausgewählte Datei ist nicht mit dieser Eigenentwicklung verknüpft.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        scanner_row = db.execute(
            "SELECT full_path, file_name, file_hash FROM idv_files WHERE id=?",
            (scanner_file_id,),
        ).fetchone()
        if not scanner_row or not scanner_row["full_path"]:
            flash("Scanner-Datei nicht mehr im Register vorhanden.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        src_path = scanner_row["full_path"]
        scan_hash = (scanner_row["file_hash"] or "").strip()
        if not os.path.isfile(src_path):
            flash(
                "Die gescannte Datei ist am hinterlegten Pfad nicht mehr "
                f"erreichbar:\n{src_path}", "error",
            )
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        try:
            src_size = os.path.getsize(src_path)
        except OSError as exc:
            current_app.logger.warning(
                "Scanner-Archivierung: Größe nicht lesbar (%s): %s", src_path, exc
            )
            flash("Scanner-Datei kann nicht gelesen werden.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))
        if src_size > _MAX_ARCHIV_UPLOAD:
            flash(
                "Scanner-Datei ist größer als "
                f"{_MAX_ARCHIV_UPLOAD // (1024 * 1024)} MB und kann nicht "
                "archiviert werden.", "error",
            )
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        original_name = scanner_row["file_name"] or os.path.basename(src_path) \
                        or "original.bin"
        safe_name = secure_filename(original_name) or "original.bin"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
        save_name = timestamp + safe_name
        folder = _archiv_upload_folder(idv_db_id)
        dest = os.path.join(folder, save_name)

        h = hashlib.sha256()
        try:
            with open(src_path, "rb") as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
        except OSError as exc:
            current_app.logger.warning(
                "Scanner-Archivierung fehlgeschlagen (IDV %s, file_id %s): %s",
                idv_db_id, scanner_file_id, exc,
            )
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            flash(
                "Übernahme der Scanner-Datei fehlgeschlagen (Lese-/Schreib"
                "fehler – Netzlaufwerk verfügbar?).", "error",
            )
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        archiv_sha256 = h.hexdigest()

        # Hash-Abgleich gegen den beim Scan aufgezeichneten Wert: verhindert,
        # dass ein zwischen Scan und Archivierung manipuliertes Original
        # unbemerkt ins Archiv übernommen wird. Bei `HASH_ERROR` (Datei damals
        # nicht hashbar) ist kein Vergleich möglich — dann wird archiviert,
        # aber mit klarem Protokollvermerk.
        if scan_hash and scan_hash not in ("HASH_ERROR",):
            if archiv_sha256.lower() != scan_hash.lower():
                try:
                    os.remove(dest)
                except OSError:
                    pass
                current_app.logger.warning(
                    "Archiv-Hash-Mismatch für IDV %s (file_id %s): "
                    "scan=%s, aktuell=%s",
                    idv_db_id, scanner_file_id, scan_hash, archiv_sha256,
                )
                flash(
                    "Archivierung abgebrochen: Der aktuelle SHA-256 der Datei "
                    "stimmt NICHT mit der Aufzeichnung aus dem letzten Scan "
                    "überein. Die Datei wurde zwischen Scan und Archivierung "
                    "verändert. Bitte erneut scannen und dann archivieren.",
                    "error",
                )
                return redirect(url_for("freigaben.erledigt_seite",
                                        freigabe_id=freigabe_id))

        archiv_pfad   = save_name
        archiv_name   = original_name

        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass

        if scan_hash and scan_hash in ("HASH_ERROR",):
            hash_note = " (Scan-Hash war HASH_ERROR, kein Abgleich möglich)"
        else:
            hash_note = ""
        befunde = (
            begruendung
            or f"Originaldatei aus Scanner-Pfad übernommen ({src_path}); "
               f"SHA-256: {archiv_sha256}{hash_note}"
        )

    else:  # quelle == "nicht_verfuegbar"
        if not begruendung:
            flash(
                "Wenn die Originaldatei nicht verfügbar ist, ist eine "
                "Begründung zwingend erforderlich.", "error"
            )
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))
        befunde = begruendung

    if datei_verfuegbar:
        aktion = "originaldatei_archiviert"
        quelle_text = (
            "vom Scanner-Pfad übernommen" if quelle == "scanner"
            else "manuell hochgeladen"
        )
        hist_kom = (
            f"Originaldatei '{archiv_name}' revisionssicher archiviert "
            f"({quelle_text}; SHA-256: {archiv_sha256})"
        )
    else:
        aktion = "originaldatei_nicht_verfuegbar"
        hist_kom = (
            "Originaldatei nicht verfügbar (z.B. Cognos / agree21Analysen). "
            f"Begründung: {befunde}"
        )

    aktion, hist_kom = _sod_log_fields(db, idv_db_id, person_id, aktion, hist_kom)

    user_name = session.get("user_name", "") or None

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE idv_freigaben
                SET status='Erledigt',
                    durchgefuehrt_von_id=?, durchgefuehrt_am=?,
                    kommentar=?, befunde=?,
                    datei_verfuegbar=?,
                    archiv_datei_pfad=?, archiv_datei_name=?, archiv_datei_sha256=?
                WHERE id=?
            """, (person_id, now, kommentar, befunde,
                  datei_verfuegbar,
                  archiv_pfad, archiv_name, archiv_sha256, freigabe_id))

            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, aktion, hist_kom, person_id, user_name),
            )

            freigegeben = _finalisiere_freigabe_wenn_komplett(c, idv_db_id, person_id, bearbeiter_name=user_name)
        return freigegeben

    freigegeben = get_writer().submit(_do, wait=True)
    if freigegeben:
        _notify_freigabe_erteilt(db, idv_db_id)
        flash("Archivierung abgeschlossen – Eigenentwicklung ist jetzt freigegeben.", "success")
    else:
        flash("Archivierungs-Schritt als Erledigt markiert.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


@bp.route("/archiv/<int:freigabe_id>")
@login_required
def archiv_download(freigabe_id):
    """Download einer archivierten Originaldatei inkl. Ownership-Check.

    Analog zu ``nachweis_download``: der Download wird über die Freigabe-ID
    an die IDV gebunden, Pfad-Traversal wird defensiv ausgeschlossen.
    """
    db  = get_db()
    row = db.execute(
        """SELECT f.archiv_datei_pfad AS pfad,
                  f.archiv_datei_name AS name,
                  f.idv_id            AS idv_db_id
             FROM idv_freigaben f
            WHERE f.id = ?""",
        (freigabe_id,),
    ).fetchone()
    if not row or not row["pfad"]:
        abort(404)
    ensure_can_read_idv(db, row["idv_db_id"])

    if (os.sep in row["pfad"] or "/" in row["pfad"] or "\\" in row["pfad"]
            or row["pfad"].startswith(".")):
        abort(404)

    folder = _archiv_upload_folder(row["idv_db_id"])
    return send_from_directory(
        folder, row["pfad"],
        as_attachment=True,
        download_name=row["name"] or row["pfad"],
    )


# ---------------------------------------------------------------------------
# Nachweis-Datei herunterladen
# ---------------------------------------------------------------------------

@bp.route("/nachweis/<int:freigabe_id>")
@login_required
def nachweis_download(freigabe_id):
    """Nachweis-Download an Freigabe-ID + Ownership gebunden (VULN-D).

    Frühere Implementierung nahm den Dateinamen aus der URL und verließ sich
    auf ``send_from_directory``, um Path-Traversal zu blocken. Das genügte,
    um das Dateisystem zu schützen, verhinderte aber nicht IDOR: jeder
    authentifizierte Benutzer konnte fremde Nachweise ziehen, sobald er den
    Dateinamen kannte/erriet.
    """
    db  = get_db()
    row = db.execute(
        """SELECT f.nachweis_datei_pfad AS pfad,
                  f.nachweis_datei_name AS name,
                  f.idv_id              AS idv_db_id
             FROM idv_freigaben f
            WHERE f.id = ?""",
        (freigabe_id,),
    ).fetchone()
    if not row or not row["pfad"]:
        abort(404)
    ensure_can_read_idv(db, row["idv_db_id"])

    # Referenz auf Scanner-Datei (gespeichert als "scanner:{id}")
    if row["pfad"].startswith("scanner:"):
        try:
            sf_id = int(row["pfad"][8:])
        except ValueError:
            abort(404)
        sf = db.execute(
            "SELECT full_path, file_name FROM idv_files WHERE id=?", (sf_id,)
        ).fetchone()
        if not sf or not sf["full_path"]:
            abort(404)
        return send_file(
            sf["full_path"],
            as_attachment=True,
            download_name=row["name"] or sf["file_name"],
        )

    # Letzter Defense-in-Depth-Check: der gespeicherte Pfad darf nur ein
    # reiner Dateiname sein – keine ``../``-Traversals aus Altbeständen.
    if os.sep in row["pfad"] or "/" in row["pfad"] or "\\" in row["pfad"] \
            or row["pfad"].startswith("."):
        abort(404)

    folder = os.path.join(current_app.instance_path, "uploads", "freigaben")
    return send_from_directory(
        folder, row["pfad"],
        as_attachment=True,
        download_name=row["name"] or row["pfad"],
    )


# ---------------------------------------------------------------------------
# Interne Hilfsfunktionen
# ---------------------------------------------------------------------------

def _notify_schritte(db, idv_db_id: int, schritte: list,
                     zugewiesen_map: dict,
                     pool_map: dict = None) -> None:
    """Sendet E-Mail an zugewiesene Personen, Pool-Mitglieder und Koordinatoren.

    ``pool_map`` ordnet jedem Schritt eine pool_id (oder None) zu. Ist ein
    Schritt einem Pool mit ≥ 1 aktiven Mitgliedern zugewiesen, werden alle
    Pool-Mitglieder benachrichtigt (außer dem IDV-Entwickler). Für 0-Mitglied-
    Pools bleibt es bei den Koordinatoren, für 1-Mitglied-Pools entspricht
    der Versand einer persönlichen Zuweisung.
    """
    pool_map = pool_map or {}
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung, idv_entwickler_id FROM idv_register WHERE id=?",
            (idv_db_id,)
        ).fetchone()
        if not idv:
            return

        from ..email_service import notify_freigabe_schritt, get_app_base_url
        from ..tokens import make_freigabe_token

        secret_key = current_app.config["SECRET_KEY"]
        base_url = get_app_base_url(db)
        entwickler_id = idv["idv_entwickler_id"] or 0

        for schritt in schritte:
            # Empfängerliste als (email, person_id) aufbauen, damit pro
            # Empfänger ein eigener, person-gebundener Magic-Link erzeugt
            # werden kann (Issue #352). person_id darf None sein — dann bekommt
            # der Empfänger einen Login-Magic-Link ohne Quick-Action.
            entries: dict[str, int | None] = {}

            def _add(email: str | None, pid: int | None):
                if not email:
                    return
                # Erste Nennung gewinnt; konkrete person_id überschreibt None.
                if email not in entries or (entries[email] is None and pid is not None):
                    entries[email] = pid

            # Koordinatoren/Admins (außer Entwickler): sind meist mehrere
            # Personen, aber als Gruppe gemeint → bewusst ohne person-Binding.
            for r in db.execute("""
                SELECT DISTINCT p.email FROM persons p
                WHERE p.aktiv=1 AND p.email IS NOT NULL
                  AND p.rolle IN ('IDV-Koordinator','IDV-Administrator')
                  AND p.id != ?
            """, (entwickler_id,)).fetchall():
                _add(r["email"], None)

            # Zugewiesene Person für diesen Schritt
            zugewiesen_id = zugewiesen_map.get(schritt)
            if zugewiesen_id:
                p = db.execute(
                    "SELECT id, email FROM persons WHERE id=? AND aktiv=1",
                    (zugewiesen_id,),
                ).fetchone()
                if p:
                    _add(p["email"], p["id"])

            # Pool-Mitglieder für diesen Schritt (Sofort-Benachrichtigung)
            pool_id = pool_map.get(schritt)
            if pool_id:
                for r in db.execute("""
                    SELECT p.id, p.email FROM freigabe_pool_members m
                    JOIN persons p ON p.id = m.person_id
                    WHERE m.pool_id = ?
                      AND p.aktiv = 1
                      AND p.email IS NOT NULL AND p.email <> ''
                      AND p.id != ?
                """, (pool_id, entwickler_id)).fetchall():
                    _add(r["email"], r["id"])

            if not entries:
                continue

            # Freigabe-ID einmal je Schritt bestimmen (nicht pro Empfänger).
            fr_id: int | None = None
            if base_url:
                fr = db.execute(
                    "SELECT id FROM idv_freigaben "
                    "WHERE idv_db_id=? AND schritt=? AND status='Ausstehend'",
                    (idv_db_id, schritt),
                ).fetchone()
                if fr:
                    fr_id = fr["id"]

            # Einzelversand pro Empfänger mit person-gebundenem Token.
            for email, pid in entries.items():
                action_url = None
                if fr_id is not None:
                    token = make_freigabe_token(secret_key, fr_id, person_id=pid)
                    action_url = f"{base_url}/quick/freigabe/{fr_id}?token={token}"
                notify_freigabe_schritt(db, idv, schritt, [email], action_url=action_url)
    except Exception as exc:
        # VULN-011: Benachrichtigungsfehler dürfen den Freigabe-Prozess nicht
        # blockieren, werden aber geloggt, damit SMTP-Konfigurationsfehler
        # nicht unentdeckt bleiben.
        current_app.logger.warning(
            "E-Mail-Benachrichtigung zu Freigabe-Schritten fehlgeschlagen: %s", exc
        )


def _notify_freigabe_erteilt(db, idv_db_id: int) -> None:
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung FROM idv_register WHERE id=?", (idv_db_id,)
        ).fetchone()
        if not idv:
            return
        recipients = [
            r["email"] for r in db.execute("""
                SELECT email FROM persons
                WHERE aktiv=1 AND email IS NOT NULL
                  AND rolle IN ('IDV-Koordinator','IDV-Administrator','IDV-Entwickler')
            """).fetchall()
            if r["email"]
        ]
        if recipients:
            from ..email_service import notify_freigabe_abgeschlossen
            notify_freigabe_abgeschlossen(db, idv, recipients)
    except Exception as exc:
        # VULN-011: siehe _notify_schritte – Fehler werden protokolliert, der
        # Workflow (IDV bereits auf Freigegeben gesetzt) läuft weiter.
        current_app.logger.warning(
            "E-Mail-Benachrichtigung 'Freigabe erteilt' fehlgeschlagen: %s", exc
        )
