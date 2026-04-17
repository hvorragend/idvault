"""Test- und Freigabeverfahren Blueprint (MaRisk AT 7.2 / BAIT / DORA)

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
                   session, current_app, send_from_directory, render_template)
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from . import login_required, own_write_required, admin_required, get_db, current_person_id
from ..security import (sanitize_html, validate_upload_mime,
                        ensure_can_read_idv, ensure_can_write_idv,
                        in_clause)

bp = Blueprint("freigaben", __name__, url_prefix="/freigaben")

_PHASE_1 = ["Fachlicher Test", "Technischer Test"]
_PHASE_2 = ["Fachliche Abnahme", "Technische Abnahme"]
_PHASE_3 = ["Archivierung Originaldatei"]
_SCHRITTE = _PHASE_1 + _PHASE_2 + _PHASE_3
_MAX_ARCHIV_UPLOAD = 256 * 1024 * 1024  # 256 MB Obergrenze für Originaldateien

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


def _phase1_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn BEIDE Phase-1-Schritte als Erledigt abgeschlossen sind."""
    ph, ph_params = in_clause(_PHASE_1)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(_PHASE_1).issubset(done)


def _phase2_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn BEIDE Phase-2-Schritte als Erledigt abgeschlossen sind."""
    ph, ph_params = in_clause(_PHASE_2)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(_PHASE_2).issubset(done)


def _phase3_komplett_erledigt(db, idv_db_id: int) -> bool:
    """True wenn der Archivierungs-Schritt (Phase 3) als Erledigt markiert ist."""
    ph, ph_params = in_clause(_PHASE_3)
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Erledigt'",
        [idv_db_id] + ph_params
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(_PHASE_3).issubset(done)


def _ensure_archiv_schritt(db, idv_db_id: int, person_id: int) -> bool:
    """Legt den Archivierungs-Schritt (Phase 3) an, sofern er noch nicht existiert.

    Wird aufgerufen, nachdem beide Phase-2-Schritte erledigt sind. Idempotent:
    ein bereits vorhandener Schritt wird nicht erneut angelegt.
    """
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt=? LIMIT 1",
        (idv_db_id, _PHASE_3[0])
    ).fetchone()
    if existing:
        return False
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO idv_freigaben
            (idv_id, schritt, status, beauftragt_von_id, beauftragt_am)
        VALUES (?, ?, 'Ausstehend', ?, ?)
    """, (idv_db_id, _PHASE_3[0], person_id, now))
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "archivierung_beauftragt",
         "Phase 3 – Archivierung Originaldatei beauftragt (nach Abschluss Phase 2)",
         person_id)
    )
    return True


def _finalisiere_freigabe_wenn_komplett(db, idv_db_id: int, person_id: int) -> bool:
    """Setzt `teststatus = 'Freigegeben'`, sobald Phase 2 UND Phase 3 komplett sind.

    Gibt True zurück, wenn die Gesamtfreigabe in diesem Aufruf erteilt wurde.
    Ruft zusätzlich die E-Mail-Benachrichtigung auf.
    """
    if not (_phase2_komplett_erledigt(db, idv_db_id)
            and _phase3_komplett_erledigt(db, idv_db_id)):
        return False
    # Nur einmal setzen: prüfen, ob teststatus bereits 'Freigegeben' ist.
    row = db.execute(
        "SELECT teststatus FROM idv_register WHERE id=?", (idv_db_id,)
    ).fetchone()
    if row and row["teststatus"] == "Freigegeben":
        return False
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        UPDATE idv_register
        SET teststatus='Freigegeben', dokumentation_vorhanden=1, aktualisiert_am=?
        WHERE id=?
    """, (now, idv_db_id))
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_erteilt",
         "Alle Freigabe-Schritte (Phase 1+2+3) erledigt – IDV freigegeben", person_id)
    )
    db.commit()
    _notify_freigabe_erteilt(db, idv_db_id)
    return True


def _int_or_none(val):
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


def _ensure_test_eintraege(db, idv_db_id: int) -> None:
    """Legt leere fachliche_testfaelle- und technischer_test-Einträge an,
    sofern noch keine existieren. Dadurch kann der Tester nach Phase-1-Start
    die Test-Seiten direkt öffnen, lesen, bearbeiten und löschen."""
    now = datetime.now(timezone.utc).isoformat()
    fachlich_exists = db.execute(
        "SELECT 1 FROM fachliche_testfaelle WHERE idv_id=? LIMIT 1", (idv_db_id,)
    ).fetchone()
    if not fachlich_exists:
        db.execute("""
            INSERT INTO fachliche_testfaelle
                (idv_id, testfall_nr, beschreibung, bewertung, erstellt_am, aktualisiert_am)
            VALUES (?, 1, NULL, 'Offen', ?, ?)
        """, (idv_db_id, now, now))
    tech_exists = db.execute(
        "SELECT 1 FROM technischer_test WHERE idv_id=? LIMIT 1", (idv_db_id,)
    ).fetchone()
    if not tech_exists:
        db.execute("""
            INSERT INTO technischer_test
                (idv_id, ergebnis, erstellt_am, aktualisiert_am)
            VALUES (?, 'Offen', ?, ?)
        """, (idv_db_id, now, now))
    db.commit()


# ---------------------------------------------------------------------------
# Shared helper: Freigabe-Schritt als Erledigt abschließen
# ---------------------------------------------------------------------------

def complete_freigabe_schritt(db, freigabe_id: int, person_id: int,
                               nachweise: str = None, kommentar: str = None) -> None:
    """Markiert einen ausstehenden Freigabe-Schritt als Erledigt und aktualisiert
    Phase-Status sowie IDV-Teststatus. Wird aus tests.py nach Speichern des Tests aufgerufen."""
    now      = datetime.now(timezone.utc).isoformat()
    freigabe = db.execute("SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        return

    idv_db_id = freigabe["idv_id"]
    schritt   = freigabe["schritt"]

    db.execute("""
        UPDATE idv_freigaben
        SET status='Erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            kommentar=?, nachweise_text=?
        WHERE id=?
    """, (person_id, now, kommentar, nachweise, freigabe_id))
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_schritt_erledigt", f"{schritt} erledigt", person_id)
    )
    db.commit()

    if schritt in _PHASE_2 and _phase2_komplett_erledigt(db, idv_db_id):
        # Nach Phase 2 Archivierungs-Schritt (Phase 3) automatisch anlegen,
        # damit die Originaldatei revisionssicher abgelegt werden kann.
        if _ensure_archiv_schritt(db, idv_db_id, person_id):
            db.commit()
        _finalisiere_freigabe_wenn_komplett(db, idv_db_id, person_id)


# ---------------------------------------------------------------------------
# Phase 1 starten: Fachlicher Test + Technischer Test (parallel)
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/starten", methods=["POST"])
@own_write_required
def starten(idv_db_id):
    """Startet Phase 1: Fachlicher Test + Technischer Test gleichzeitig."""
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
            flash("Freigabeverfahren nur für wesentliche IDVs erforderlich.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Guard: Phase 1 darf noch nicht gestartet sein
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt IN (?,?)",
        (idv_db_id, _PHASE_1[0], _PHASE_1[1])
    ).fetchone()
    if existing:
        flash("Phase 1 (Tests) wurde bereits gestartet.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    zugewiesen_fachlich  = _int_or_none(request.form.get("zugewiesen_fachlicher_test"))
    zugewiesen_technisch = _int_or_none(request.form.get("zugewiesen_technischer_test"))

    # Beide Phase-1-Schritte gleichzeitig anlegen
    for schritt, zugewiesen in [(_PHASE_1[0], zugewiesen_fachlich),
                                 (_PHASE_1[1], zugewiesen_technisch)]:
        db.execute("""
            INSERT INTO idv_freigaben
                (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id)
            VALUES (?, ?, 'Ausstehend', ?, ?, ?)
        """, (idv_db_id, schritt, person_id, now, zugewiesen))

    # Leere Test-Einträge automatisch mit anlegen, damit der Tester sofort
    # bearbeiten kann. Wenn bereits vorhanden (z.B. nach Verfahrens-Reset),
    # bleibt der bestehende Eintrag erhalten.
    _ensure_test_eintraege(db, idv_db_id)

    db.execute(
        "UPDATE idv_register SET teststatus='Freigabe ausstehend', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_gestartet",
         "Freigabeverfahren gestartet – Phase 1: Fachlicher Test + Technischer Test (parallel)",
         person_id)
    )
    db.commit()

    _notify_schritte(db, idv_db_id, _PHASE_1,
                     {_PHASE_1[0]: zugewiesen_fachlich, _PHASE_1[1]: zugewiesen_technisch})

    flash("Phase 1 gestartet: Fachlicher Test und Technischer Test laufen parallel.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Phase 2 starten: Fachliche Abnahme + Technische Abnahme (parallel)
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/abnahme-starten", methods=["POST"])
@own_write_required
def abnahme_starten(idv_db_id):
    """Startet Phase 2: Fachliche Abnahme + Technische Abnahme – erst nach vollständiger Phase 1."""
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    if not _phase1_komplett_erledigt(db, idv_db_id):
        flash("Phase 2 kann erst gestartet werden, wenn beide Phase-1-Tests erledigt sind.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Guard: Phase 2 darf noch nicht gestartet sein
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt IN (?,?)",
        (idv_db_id, _PHASE_2[0], _PHASE_2[1])
    ).fetchone()
    if existing:
        flash("Phase 2 (Abnahmen) wurde bereits gestartet.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    zugewiesen_fachlich  = _int_or_none(request.form.get("zugewiesen_fachliche_abnahme"))
    zugewiesen_technisch = _int_or_none(request.form.get("zugewiesen_technische_abnahme"))

    for schritt, zugewiesen in [(_PHASE_2[0], zugewiesen_fachlich),
                                 (_PHASE_2[1], zugewiesen_technisch)]:
        db.execute("""
            INSERT INTO idv_freigaben
                (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id)
            VALUES (?, ?, 'Ausstehend', ?, ?, ?)
        """, (idv_db_id, schritt, person_id, now, zugewiesen))

    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_phase2_gestartet",
         "Phase 2 gestartet: Fachliche Abnahme + Technische Abnahme (parallel)", person_id)
    )
    db.commit()

    _notify_schritte(db, idv_db_id, _PHASE_2,
                     {_PHASE_2[0]: zugewiesen_fachlich, _PHASE_2[1]: zugewiesen_technisch})

    flash("Phase 2 gestartet: Fachliche Abnahme und Technische Abnahme laufen parallel.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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
               p_z.nachname || ', ' || p_z.vorname AS zugewiesen_an
        FROM idv_freigaben f
        LEFT JOIN persons p_d ON f.durchgefuehrt_von_id = p_d.id
        LEFT JOIN persons p_z ON f.zugewiesen_an_id     = p_z.id
        WHERE f.id = ?
    """, (freigabe_id,)).fetchone()
    if not freigabe:
        flash("Freigabe-Schritt nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))
    ensure_can_read_idv(db, freigabe["idv_id"])
    idv = db.execute("SELECT * FROM idv_register WHERE id=?", (freigabe["idv_id"],)).fetchone()
    if not idv:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

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

    # Phase 3: Archivierungs-Schritt → spezialisierte Maske
    if freigabe["schritt"] in _PHASE_3:
        readonly = freigabe["status"] != "Ausstehend"
        scanner_dateien = _verfuegbare_scanner_dateien(db, idv["id"]) if not readonly else []
        return render_template("freigaben/archiv_form.html",
                               freigabe=freigabe, idv=idv, readonly=readonly,
                               scanner_dateien=scanner_dateien)

    # Phase 2: Abnahmeformular – bearbeitbar wenn Ausstehend, sonst Lesemodus
    readonly = freigabe["status"] != "Ausstehend"
    return render_template("freigaben/bestanden_form.html",
                           freigabe=freigabe, idv=idv, readonly=readonly)


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

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("idv.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser IDV eingetragen "
            "und dürfen keine Freigabe-Schritte abschließen.", "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

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
            flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")

    db.execute("""
        UPDATE idv_freigaben
        SET status='Erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            kommentar=?, nachweise_text=?, nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, kommentar, nachweise, nachweis_pfad, nachweis_name, freigabe_id))

    schritt = freigabe["schritt"]
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_schritt_erledigt", f"{schritt} erledigt", person_id)
    )
    db.commit()

    # Prüfen ob Phase 2 jetzt vollständig abgeschlossen ist → Archivierung starten
    if schritt in _PHASE_2 and _phase2_komplett_erledigt(db, idv_db_id):
        neu_angelegt = _ensure_archiv_schritt(db, idv_db_id, person_id)
        if neu_angelegt:
            db.commit()
        freigegeben = _finalisiere_freigabe_wenn_komplett(db, idv_db_id, person_id)
        if freigegeben:
            flash("Alle Freigabe-Schritte erledigt – IDV ist jetzt freigegeben.", "success")
        else:
            flash(
                f"'{schritt}' erledigt – Phase 2 vollständig. "
                "Bitte nun die Originaldatei revisionssicher archivieren "
                "(Phase 3).", "success"
            )
    elif schritt in _PHASE_1 and _phase1_komplett_erledigt(db, idv_db_id):
        flash(f"'{schritt}' erledigt – Phase 1 vollständig. Bitte Phase 2 starten.", "success")
    else:
        flash(f"'{schritt}' als Erledigt markiert.", "success")

    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("idv.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler eingetragen "
            "und dürfen keine Freigabe-Schritte ablehnen.", "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

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

    db.execute("""
        UPDATE idv_freigaben
        SET status='Nicht erledigt', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            befunde=?, kommentar=?, nachweise_text=?,
            nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, befunde, kommentar, nachweise,
          nachweis_pfad, nachweis_name, freigabe_id))

    # Bearbeitungsstatus zurücksetzen
    db.execute(
        "UPDATE idv_register SET teststatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_abgelehnt",
         f"{freigabe['schritt']} nicht erledigt. Befunde: {befunde}", person_id)
    )
    db.commit()

    flash(f"'{freigabe['schritt']}' nicht erledigt.", "warning")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Admin: Verfahren abbrechen
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/abbrechen", methods=["POST"])
@admin_required
def abbrechen(idv_db_id):
    """Admin bricht das laufende Freigabeverfahren ab."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    kommentar = request.form.get("abbruch_kommentar", "").strip() or None

    offene = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND status='Ausstehend'",
        (idv_db_id,)
    ).fetchall()

    if not offene:
        flash("Kein laufendes Freigabeverfahren gefunden.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    for row in offene:
        db.execute("""
            UPDATE idv_freigaben
            SET status='Abgebrochen', abgebrochen_von_id=?, abgebrochen_am=?, abbruch_kommentar=?
            WHERE id=?
        """, (person_id, now, kommentar, row["id"]))

    db.execute(
        "UPDATE idv_register SET teststatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_abgebrochen",
         "Freigabeverfahren durch Administrator abgebrochen."
         + (f" Grund: {kommentar}" if kommentar else ""),
         person_id)
    )
    db.commit()

    flash("Freigabeverfahren wurde abgebrochen.", "warning")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Einzelnen Freigabe-Schritt wieder anlegen (nach Löschung)
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/schritt-anlegen", methods=["POST"])
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
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Für Phase 2 ist erforderlich, dass Phase 1 komplett erledigt ist
    if schritt in _PHASE_2 and not _phase1_komplett_erledigt(db, idv_db_id):
        flash("Phase-2-Schritte können erst nach kompletter Phase 1 angelegt werden.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Für Phase 3 (Archivierung) ist erforderlich, dass Phase 2 komplett erledigt ist
    if schritt in _PHASE_3 and not _phase2_komplett_erledigt(db, idv_db_id):
        flash("Archivierungs-Schritt kann erst nach kompletter Phase 2 angelegt werden.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Duplikats-Guard: Schritt darf nicht bereits existieren
    existing = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND schritt=? LIMIT 1",
        (idv_db_id, schritt)
    ).fetchone()
    if existing:
        flash(f"'{schritt}' existiert bereits.", "info")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    zugewiesen = _int_or_none(request.form.get("zugewiesen_an_id"))
    db.execute("""
        INSERT INTO idv_freigaben
            (idv_id, schritt, status, beauftragt_von_id, beauftragt_am, zugewiesen_an_id)
        VALUES (?, ?, 'Ausstehend', ?, ?, ?)
    """, (idv_db_id, schritt, person_id, now, zugewiesen))

    # Für Phase-1-Schritte auch den Test-Eintrag sicherstellen
    if schritt in _PHASE_1:
        _ensure_test_eintraege(db, idv_db_id)

    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_schritt_angelegt", f"{schritt} erneut angelegt", person_id)
    )
    db.commit()

    _notify_schritte(db, idv_db_id, [schritt], {schritt: zugewiesen})
    flash(f"'{schritt}' wurde angelegt.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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
        return redirect(url_for("idv.list_idv"))
    idv_db_id = freigabe["idv_id"]
    schritt   = freigabe["schritt"]
    db.execute("DELETE FROM idv_freigaben WHERE id=?", (freigabe_id,))
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_schritt_geloescht", f"{schritt} gelöscht", person_id)
    )
    db.commit()
    flash(f"'{schritt}' wurde gelöscht.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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
        return redirect(url_for("idv.list_idv"))

    idv_db_id = freigabe["idv_id"]
    ensure_can_write_idv(db, idv_db_id)

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser IDV eingetragen "
            "und dürfen die Archivierung nicht abschließen.", "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    quelle = (request.form.get("archiv_quelle") or "upload").strip().lower()
    if quelle not in ("upload", "scanner", "nicht_verfuegbar"):
        quelle = "upload"
    # Rückwärtskompatibilität: ältere Formulare schicken nur datei_verfuegbar
    if "archiv_quelle" not in request.form:
        quelle = "upload" if request.form.get("datei_verfuegbar", "1") == "1" else "nicht_verfuegbar"

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
            flash("Die ausgewählte Datei ist nicht mit dieser IDV verknüpft.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        scanner_row = db.execute(
            "SELECT full_path, file_name FROM idv_files WHERE id=?",
            (scanner_file_id,),
        ).fetchone()
        if not scanner_row or not scanner_row["full_path"]:
            flash("Scanner-Datei nicht mehr im Register vorhanden.", "error")
            return redirect(url_for("freigaben.erledigt_seite",
                                    freigabe_id=freigabe_id))

        src_path = scanner_row["full_path"]
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
        archiv_pfad   = save_name
        archiv_name   = original_name

        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass

        befunde = (
            begruendung
            or f"Originaldatei aus Scanner-Pfad übernommen ({src_path}); "
               f"SHA-256: {archiv_sha256}"
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

    db.execute("""
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
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, aktion, hist_kom, person_id)
    )
    db.commit()

    freigegeben = _finalisiere_freigabe_wenn_komplett(db, idv_db_id, person_id)
    if freigegeben:
        flash("Archivierung abgeschlossen – IDV ist jetzt freigegeben.", "success")
    else:
        flash("Archivierungs-Schritt als Erledigt markiert.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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
                     zugewiesen_map: dict) -> None:
    """Sendet E-Mail an zugewiesene Personen und Koordinatoren für die gegebenen Schritte."""
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung, idv_entwickler_id FROM idv_register WHERE id=?",
            (idv_db_id,)
        ).fetchone()
        if not idv:
            return

        from ..email_service import notify_freigabe_schritt

        for schritt in schritte:
            recipient_set = set()

            # Koordinatoren/Admins (außer Entwickler)
            for r in db.execute("""
                SELECT DISTINCT p.email FROM persons p
                WHERE p.aktiv=1 AND p.email IS NOT NULL
                  AND p.rolle IN ('IDV-Koordinator','IDV-Administrator')
                  AND p.id != ?
            """, (idv["idv_entwickler_id"] or 0,)).fetchall():
                if r["email"]:
                    recipient_set.add(r["email"])

            # Zugewiesene Person für diesen Schritt
            zugewiesen_id = zugewiesen_map.get(schritt)
            if zugewiesen_id:
                p = db.execute(
                    "SELECT email FROM persons WHERE id=? AND aktiv=1", (zugewiesen_id,)
                ).fetchone()
                if p and p["email"]:
                    recipient_set.add(p["email"])

            recipients = list(recipient_set)
            if recipients:
                notify_freigabe_schritt(db, idv, schritt, recipients)
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
