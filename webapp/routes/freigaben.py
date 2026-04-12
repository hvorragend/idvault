"""Test- und Freigabeverfahren Blueprint (MaRisk AT 7.2 / BAIT / DORA)

Schrittfolge: Fachlicher Test → Technischer Test → Fachliche Abnahme → Technische Abnahme
Funktionstrennung: Entwickler der IDV darf keine Schritte abschließen.
Nur wesentliche IDVs durchlaufen dieses Verfahren – außer wenn letzte_aenderungsart = 'unwesentlich'.
"""
import os
from flask import (Blueprint, request, flash, redirect, url_for,
                   session, current_app, send_from_directory)
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from . import login_required, own_write_required, admin_required, get_db, current_person_id

bp = Blueprint("freigaben", __name__, url_prefix="/freigaben")

_SCHRITTE = [
    "Fachlicher Test",
    "Technischer Test",
    "Fachliche Abnahme",
    "Technische Abnahme",
]

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


def _save_upload(file) -> tuple[str, str] | tuple[None, None]:
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
    """True wenn das Testverfahren für diese IDV/Version erforderlich ist.
    Nicht erforderlich bei unwesentlichen Änderungen (neue Version ohne wesentliche Änderung)."""
    if not _ist_wesentlich(db, idv_db_id):
        return False
    row = db.execute(
        "SELECT letzte_aenderungsart FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if row and row["letzte_aenderungsart"] == "unwesentlich":
        return False
    return True


def _funktionstrennung_ok(db, idv_db_id: int, person_id: int) -> bool:
    """True wenn der angemeldete User NICHT der eingetragene Entwickler ist.
    Admins sind von der Funktionstrennung ausgenommen."""
    from flask import session
    from . import ROLE_ADMIN
    if session.get("user_role") == ROLE_ADMIN:
        return True
    row = db.execute(
        "SELECT idv_entwickler_id FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not row or row["idv_entwickler_id"] is None:
        return True
    return row["idv_entwickler_id"] != person_id


# ---------------------------------------------------------------------------
# Verfahren starten
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/starten", methods=["POST"])
@own_write_required
def starten(idv_db_id):
    """Startet den ersten Freigabe-Schritt (Fachlicher Test)."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    # Guard: IDV muss das Testverfahren erfordern
    if not _testverfahren_erforderlich(db, idv_db_id):
        row = db.execute("SELECT letzte_aenderungsart FROM idv_register WHERE id=?",
                         (idv_db_id,)).fetchone()
        if row and row["letzte_aenderungsart"] == "unwesentlich":
            flash("Kein Testverfahren erforderlich – letzte Änderung war als unwesentlich eingestuft.", "info")
        else:
            flash("Freigabeverfahren nur für wesentliche IDVs erforderlich.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Guard: kein offener Schritt vorhanden
    offen = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND status='Ausstehend'",
        (idv_db_id,)
    ).fetchone()
    if offen:
        flash("Ein Freigabe-Schritt ist bereits offen.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Freigabeanforderer und Versionskommentar
    freigabeanforderer_id = request.form.get("freigabeanforderer_id") or None
    if freigabeanforderer_id:
        try:
            freigabeanforderer_id = int(freigabeanforderer_id)
        except ValueError:
            freigabeanforderer_id = None
    versions_kommentar = request.form.get("versions_kommentar", "").strip() or None

    # Ersten Schritt anlegen
    db.execute("""
        INSERT INTO idv_freigaben
            (idv_id, schritt, status, beauftragt_von_id, beauftragt_am,
             freigabeanforderer_id, versions_kommentar)
        VALUES (?, ?, 'Ausstehend', ?, ?, ?, ?)
    """, (idv_db_id, _SCHRITTE[0], person_id, now, freigabeanforderer_id, versions_kommentar))

    # Bearbeitungsstatus auf "Freigabe ausstehend"
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='Freigabe ausstehend', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_gestartet",
         f"Freigabeverfahren gestartet – Schritt 1: {_SCHRITTE[0]}"
         + (f" | Anforderer: {freigabeanforderer_id}" if freigabeanforderer_id else "")
         + (f" | {versions_kommentar}" if versions_kommentar else ""),
         person_id)
    )
    db.commit()

    # E-Mail an Koordinatoren / Fachverantwortliche (nicht an Entwickler)
    # und ggf. an den Freigabeanforderer
    _notify_naechster_schritt(db, idv_db_id, _SCHRITTE[0],
                              freigabeanforderer_id=freigabeanforderer_id,
                              versions_kommentar=versions_kommentar)

    flash(f"Freigabeverfahren gestartet – Schritt '{_SCHRITTE[0]}' offen.", "success")
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

    # Funktionstrennung prüfen
    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser IDV eingetragen "
            "und dürfen keine Freigabe-Schritte abschließen.",
            "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    kommentar    = request.form.get("kommentar", "").strip() or None
    nachweise    = request.form.get("nachweise_text", "").strip() or None

    # Datei-Upload
    nachweis_pfad = freigabe["nachweis_datei_pfad"] if freigabe["nachweis_datei_pfad"] else None
    nachweis_name = freigabe["nachweis_datei_name"] if freigabe["nachweis_datei_name"] else None
    upload_file = request.files.get("nachweis_datei")
    if upload_file and upload_file.filename:
        saved_name, orig_name = _save_upload(upload_file)
        if saved_name:
            nachweis_pfad = saved_name
            nachweis_name = orig_name
        else:
            flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")

    # Schritt abschließen
    db.execute("""
        UPDATE idv_freigaben
        SET status='Bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            kommentar=?, nachweise_text=?, nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, kommentar, nachweise, nachweis_pfad, nachweis_name, freigabe_id))

    # Nächsten Schritt bestimmen
    aktueller_idx = _SCHRITTE.index(freigabe["schritt"]) if freigabe["schritt"] in _SCHRITTE else -1
    naechster_idx = aktueller_idx + 1

    if naechster_idx < len(_SCHRITTE):
        naechster = _SCHRITTE[naechster_idx]
        db.execute("""
            INSERT INTO idv_freigaben (idv_id, schritt, status, beauftragt_von_id, beauftragt_am)
            VALUES (?, ?, 'Ausstehend', ?, ?)
        """, (idv_db_id, naechster, person_id, now))
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "freigabe_schritt_bestanden",
             f"{freigabe['schritt']} bestanden → {naechster} offen", person_id)
        )
        db.commit()
        _notify_naechster_schritt(db, idv_db_id, naechster)
        flash(f"'{freigabe['schritt']}' bestanden – nächster Schritt: '{naechster}'.", "success")
    else:
        # Alle 4 Schritte bestanden → Freigabe erteilt
        db.execute("""
            UPDATE idv_register
            SET bearbeitungsstatus='Freigegeben', dokumentationsstatus='Dokumentiert',
                aktualisiert_am=?
            WHERE id=?
        """, (now, idv_db_id))
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "freigabe_erteilt",
             "Alle 4 Freigabe-Schritte bestanden – IDV freigegeben", person_id)
        )
        db.commit()
        _notify_freigabe_erteilt(db, idv_db_id)
        flash("Alle Freigabe-Schritte bestanden – IDV ist jetzt freigegeben.", "success")

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
            "und dürfen keine Freigabe-Schritte ablehnen.",
            "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    befunde      = request.form.get("befunde", "").strip() or None
    kommentar    = request.form.get("kommentar", "").strip() or None
    nachweise    = request.form.get("nachweise_text", "").strip() or None

    # Datei-Upload
    nachweis_pfad = None
    nachweis_name = None
    upload_file = request.files.get("nachweis_datei")
    if upload_file and upload_file.filename:
        saved_name, orig_name = _save_upload(upload_file)
        if saved_name:
            nachweis_pfad = saved_name
            nachweis_name = orig_name

    db.execute("""
        UPDATE idv_freigaben
        SET status='Nicht bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            befunde=?, kommentar=?, nachweise_text=?,
            nachweis_datei_pfad=?, nachweis_datei_name=?
        WHERE id=?
    """, (person_id, now, befunde, kommentar, nachweise,
          nachweis_pfad, nachweis_name, freigabe_id))

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

    flash(f"'{freigabe['schritt']}' nicht bestanden – Verfahren unterbrochen.", "warning")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Admin: Verfahren abbrechen
# ---------------------------------------------------------------------------

@bp.route("/idv/<int:idv_db_id>/abbrechen", methods=["POST"])
@admin_required
def abbrechen(idv_db_id):
    """Admin bricht das laufende Test- und Freigabeverfahren ab."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    kommentar = request.form.get("abbruch_kommentar", "").strip() or None

    # Alle offenen Schritte auf 'Abgebrochen' setzen
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
            SET status='Abgebrochen', abgebrochen_von_id=?, abgebrochen_am=?,
                abbruch_kommentar=?
            WHERE id=?
        """, (person_id, now, kommentar, row["id"]))

    # Bearbeitungsstatus zurücksetzen
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_abgebrochen",
         f"Freigabeverfahren durch Administrator abgebrochen."
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
    """Lädt eine hochgeladene Nachweis-Datei herunter."""
    folder = os.path.join(current_app.instance_path, "uploads", "freigaben")
    return send_from_directory(folder, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Interne Hilfsfunktionen
# ---------------------------------------------------------------------------

def _notify_naechster_schritt(db, idv_db_id: int, schritt: str,
                               freigabeanforderer_id=None,
                               versions_kommentar=None) -> None:
    """Sendet E-Mail an Koordinatoren/Fachverantwortliche und ggf. Freigabeanforderer."""
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung, idv_entwickler_id FROM idv_register WHERE id=?",
            (idv_db_id,)
        ).fetchone()
        if not idv:
            return

        recipient_set = set()

        # Standard: Koordinatoren/Admins (außer Entwickler)
        for r in db.execute("""
            SELECT DISTINCT p.email FROM persons p
            WHERE p.aktiv=1 AND p.email IS NOT NULL
              AND p.rolle IN ('IDV-Koordinator','IDV-Administrator')
              AND p.id != ?
        """, (idv["idv_entwickler_id"] or 0,)).fetchall():
            if r["email"]:
                recipient_set.add(r["email"])

        # Freigabeanforderer zusätzlich benachrichtigen
        if freigabeanforderer_id:
            anf = db.execute(
                "SELECT email, nachname, vorname FROM persons WHERE id=? AND aktiv=1",
                (freigabeanforderer_id,)
            ).fetchone()
            if anf and anf["email"]:
                recipient_set.add(anf["email"])

        recipients = list(recipient_set)
        if not recipients:
            return

        from ..email_service import notify_freigabe_schritt
        notify_freigabe_schritt(db, idv, schritt, recipients,
                                versions_kommentar=versions_kommentar)
    except Exception:
        pass


def _notify_freigabe_erteilt(db, idv_db_id: int) -> None:
    """Sendet Abschluss-E-Mail wenn alle 4 Schritte bestanden."""
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
        if not recipients:
            return

        from ..email_service import notify_freigabe_abgeschlossen
        notify_freigabe_abgeschlossen(db, idv, recipients)
    except Exception:
        pass
