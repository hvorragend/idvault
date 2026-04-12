"""Test- und Freigabeverfahren Blueprint (MaRisk AT 7.2 / BAIT / DORA)

Zwei parallele Phasen:
  Phase 1 (parallel): Fachlicher Test + Technischer Test
  Phase 2 (parallel): Fachliche Abnahme + Technische Abnahme

Phase 2 startet erst, wenn BEIDE Phase-1-Schritte als 'Bestanden' markiert sind.
Funktionstrennung: Entwickler der IDV darf keine Schritte abschließen.
Nur wesentliche IDVs mit wesentlicher Änderung durchlaufen dieses Verfahren.
"""
import os
from flask import (Blueprint, request, flash, redirect, url_for,
                   session, current_app, send_from_directory)
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from . import login_required, own_write_required, admin_required, get_db, current_person_id

bp = Blueprint("freigaben", __name__, url_prefix="/freigaben")

_PHASE_1 = ["Fachlicher Test", "Technischer Test"]
_PHASE_2 = ["Fachliche Abnahme", "Technische Abnahme"]
_SCHRITTE = _PHASE_1 + _PHASE_2

_WESENTLICH_SQL = """(
    r.steuerungsrelevant = 1 OR r.rechnungslegungsrelevant = 1 OR r.dora_kritisch_wichtig = 1
    OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1)
)"""

_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "xlsx", "xls", "docx", "doc",
                       "txt", "csv", "zip"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


def _upload_folder() -> str:
    folder = os.path.join(current_app.instance_path, "uploads", "freigaben")
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_upload(file):
    """Speichert eine hochgeladene Datei. Gibt (relativer_pfad, originaldateiname) zurück."""
    if not file or not file.filename:
        return None, None
    if not _allowed_file(file.filename):
        return None, None
    original_name = file.filename
    safe_name = secure_filename(original_name)
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


def _phase1_komplett_bestanden(db, idv_db_id: int) -> bool:
    """True wenn BEIDE Phase-1-Schritte als Bestanden abgeschlossen sind."""
    ph = ",".join("?" * len(_PHASE_1))
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Bestanden'",
        [idv_db_id] + _PHASE_1
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(_PHASE_1).issubset(done)


def _phase2_komplett_bestanden(db, idv_db_id: int) -> bool:
    """True wenn BEIDE Phase-2-Schritte als Bestanden abgeschlossen sind."""
    ph = ",".join("?" * len(_PHASE_2))
    rows = db.execute(
        f"SELECT schritt FROM idv_freigaben WHERE idv_id=? AND schritt IN ({ph}) AND status='Bestanden'",
        [idv_db_id] + _PHASE_2
    ).fetchall()
    done = {r["schritt"] for r in rows}
    return set(_PHASE_2).issubset(done)


def _int_or_none(val):
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Phase 1 starten: Fachlicher Test + Technischer Test (parallel)
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/starten", methods=["POST"])
@own_write_required
def starten(idv_db_id):
    """Startet Phase 1: Fachlicher Test + Technischer Test gleichzeitig."""
    db        = get_db()
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

    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='Freigabe ausstehend', aktualisiert_am=? WHERE id=?",
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
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    if not _phase1_komplett_bestanden(db, idv_db_id):
        flash("Phase 2 kann erst gestartet werden, wenn beide Phase-1-Tests bestanden sind.", "warning")
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
# Schritt abschließen
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/abschliessen", methods=["POST"])
@own_write_required
def abschliessen(freigabe_id):
    """Schließt einen Freigabe-Schritt als 'Bestanden' ab."""
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

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser IDV eingetragen "
            "und dürfen keine Freigabe-Schritte abschließen.", "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    kommentar = request.form.get("kommentar", "").strip() or None
    nachweise = request.form.get("nachweise_text", "").strip() or None

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
        SET status='Bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            kommentar=?, nachweise_text=?, nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, kommentar, nachweise, nachweis_pfad, nachweis_name, freigabe_id))

    schritt = freigabe["schritt"]
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_schritt_bestanden", f"{schritt} bestanden", person_id)
    )
    db.commit()

    # Prüfen ob Phase 2 jetzt vollständig abgeschlossen ist → IDV freigeben
    if schritt in _PHASE_2 and _phase2_komplett_bestanden(db, idv_db_id):
        db.execute("""
            UPDATE idv_register
            SET bearbeitungsstatus='Freigegeben', dokumentationsstatus='Dokumentiert',
                aktualisiert_am=?
            WHERE id=?
        """, (now, idv_db_id))
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "freigabe_erteilt",
             "Alle 4 Freigabe-Schritte (Phase 1+2) bestanden – IDV freigegeben", person_id)
        )
        db.commit()
        _notify_freigabe_erteilt(db, idv_db_id)
        flash("Alle Freigabe-Schritte bestanden – IDV ist jetzt freigegeben.", "success")
    elif schritt in _PHASE_1 and _phase1_komplett_bestanden(db, idv_db_id):
        flash(f"'{schritt}' bestanden – Phase 1 vollständig. Bitte Phase 2 starten.", "success")
    else:
        flash(f"'{schritt}' als Bestanden markiert.", "success")

    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Schritt ablehnen
# ---------------------------------------------------------------------------

@bp.route("/<int:freigabe_id>/ablehnen", methods=["POST"])
@own_write_required
def ablehnen(freigabe_id):
    """Markiert einen Freigabe-Schritt als 'Nicht bestanden'."""
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

    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler eingetragen "
            "und dürfen keine Freigabe-Schritte ablehnen.", "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    befunde   = request.form.get("befunde", "").strip() or None
    kommentar = request.form.get("kommentar", "").strip() or None
    nachweise = request.form.get("nachweise_text", "").strip() or None

    nachweis_pfad = nachweis_name = None
    upload_file = request.files.get("nachweis_datei")
    if upload_file and upload_file.filename:
        saved, orig = _save_upload(upload_file)
        if saved:
            nachweis_pfad, nachweis_name = saved, orig

    db.execute("""
        UPDATE idv_freigaben
        SET status='Nicht bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            befunde=?, kommentar=?, nachweise_text=?,
            nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, befunde, kommentar, nachweise,
          nachweis_pfad, nachweis_name, freigabe_id))

    # Bearbeitungsstatus zurücksetzen
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_abgelehnt",
         f"{freigabe['schritt']} nicht bestanden. Befunde: {befunde}", person_id)
    )
    db.commit()

    flash(f"'{freigabe['schritt']}' nicht bestanden.", "warning")
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
        "UPDATE idv_register SET bearbeitungsstatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
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
# Nachweis-Datei herunterladen
# ---------------------------------------------------------------------------

@bp.route("/nachweis/<path:filename>")
@login_required
def nachweis_download(filename):
    folder = os.path.join(current_app.instance_path, "uploads", "freigaben")
    return send_from_directory(folder, filename, as_attachment=True)


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
    except Exception:
        pass


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
    except Exception:
        pass
