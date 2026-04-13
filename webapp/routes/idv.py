from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from . import (login_required, write_access_required, own_write_required, admin_required,
               get_db, can_write, can_create, can_read_all, current_person_id)
import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import (create_idv, update_idv, change_status, search_idv,
                get_klassifizierungen, get_wesentlichkeitskriterien,
                get_idv_wesentlichkeit, save_idv_wesentlichkeit)

# Dateierweiterung → IDV-Typ-Vorschlag (gespiegelt aus scanner.py)
_EXT_TO_TYP = {
    ".xlsx": "Excel-Tabelle", ".xlsm": "Excel-Makro", ".xlsb": "Excel-Makro",
    ".xls":  "Excel-Tabelle", ".xltm": "Excel-Makro", ".xltx": "Excel-Tabelle",
    ".accdb": "Access-Datenbank", ".mdb": "Access-Datenbank",
    ".accde": "Access-Datenbank", ".accdr": "Access-Datenbank",
    ".py": "Python-Skript", ".r": "Sonstige", ".rmd": "Sonstige",
    ".sql": "SQL-Skript", ".pbix": "Power-BI-Bericht", ".pbit": "Power-BI-Bericht",
}

bp = Blueprint("idv", __name__, url_prefix="/idv")


def _form_lookups(db):
    return {
        "org_units":          db.execute("SELECT * FROM org_units WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "persons":            db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall(),
        "geschaeftsprozesse": db.execute("SELECT * FROM geschaeftsprozesse WHERE aktiv=1 ORDER BY gp_nummer").fetchall(),
        "plattformen":        db.execute("SELECT * FROM plattformen WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "risikoklassen":      db.execute("SELECT * FROM risikoklassen ORDER BY sort_order").fetchall(),
        # Konfigurierbare Klassifizierungen
        "idv_typen":               get_klassifizierungen(db, "idv_typ"),
        "pruefintervalle":         get_klassifizierungen(db, "pruefintervall_monate"),
        "nutzungsfrequenzen":      get_klassifizierungen(db, "nutzungsfrequenz"),
        "gda_stufen":              get_klassifizierungen(db, "gda_stufen"),
        # Konfigurierbare Wesentlichkeitskriterien
        "wesentlichkeitskriterien": get_wesentlichkeitskriterien(db, nur_aktive=True),
    }


def _int_or_none(val):
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


# ── Liste ──────────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def list_idv():
    db      = get_db()
    q       = request.args.get("q", "")
    status  = request.args.get("status", "")
    filt    = request.args.get("filter", "")
    oe_id   = _int_or_none(request.args.get("oe_id"))
    fv_id   = _int_or_none(request.args.get("fv_id"))
    share_root = request.args.get("share_root", "").strip()

    _WESENTLICH = """(
        v.steuerungsrelevant = 'Ja' OR v.rl_relevant = 'Ja' OR v.dora_kritisch = 'Ja'
        OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1)
    )"""

    # Alle Bedingungen und Parameter als positionale Listen aufbauen
    where_parts = []
    params      = []

    if q:
        where_parts.append("(v.idv_id LIKE ? OR v.bezeichnung LIKE ? OR v.geschaeftsprozess LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if status:
        where_parts.append("v.status = ?")
        params.append(status)
    if oe_id:
        where_parts.append("r.org_unit_id = ?")
        params.append(oe_id)
    if fv_id:
        where_parts.append("r.fachverantwortlicher_id = ?")
        params.append(fv_id)
    if share_root:
        where_parts.append(
            "r.file_id IN (SELECT id FROM idv_files WHERE share_root = ?)"
        )
        params.append(share_root)

    # Spezialfilter
    if filt in ("kritisch", "wesentlich"):
        where_parts.append(_WESENTLICH)
    elif filt == "nicht_wesentlich":
        where_parts.append(f"NOT {_WESENTLICH}")
    elif filt == "steuerung":
        where_parts.append("v.steuerungsrelevant = 'Ja'")
    elif filt == "dora":
        where_parts.append("v.dora_kritisch = 'Ja'")
    elif filt == "ueberfaellig":
        where_parts.append("v.pruefstatus = 'ÜBERFÄLLIG'")
    elif filt == "unvollstaendig":
        ids = [r["idv_id"] for r in db.execute("SELECT idv_id FROM v_unvollstaendige_idvs").fetchall()]
        if ids:
            where_parts.append(f"r.idv_id IN ({','.join(['?']*len(ids))})")
            params.extend(ids)
        else:
            where_parts.append("1=0")

    # Rollenbasierte Sichtbarkeit
    person_id = current_person_id()
    if not can_read_all() and person_id:
        where_parts.append("""(
            r.fachverantwortlicher_id = ?
            OR r.idv_entwickler_id   = ?
            OR r.idv_koordinator_id  = ?
            OR r.stellvertreter_id   = ?
        )""")
        params += [person_id, person_id, person_id, person_id]

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT r.*, v.*,
          CASE WHEN {_WESENTLICH} THEN 1 ELSE 0 END AS ist_wesentlich,
          EXISTS(SELECT 1 FROM idv_register x WHERE x.vorgaenger_idv_id = r.id) AS hat_nachfolger,
          (CASE WHEN r.file_id IS NOT NULL THEN 1 ELSE 0 END
           + (SELECT COUNT(*) FROM idv_file_links lnk WHERE lnk.idv_db_id = r.id)) AS datei_anzahl
        FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        {where_sql}
        ORDER BY ist_wesentlich DESC, v.bezeichnung
    """
    idvs = db.execute(sql, params).fetchall()

    # Filter-Optionen für Dropdowns
    org_units = db.execute(
        "SELECT id, kuerzel, bezeichnung FROM org_units WHERE aktiv=1 ORDER BY bezeichnung"
    ).fetchall()
    persons_fv = db.execute(
        "SELECT id, nachname, vorname FROM persons WHERE aktiv=1 ORDER BY nachname"
    ).fetchall()
    share_roots = [
        r["share_root"] for r in db.execute(
            "SELECT DISTINCT share_root FROM idv_files WHERE share_root IS NOT NULL AND status='active' ORDER BY share_root"
        ).fetchall()
    ]

    from . import ROLE_ADMIN
    is_admin = (session.get("user_role") == ROLE_ADMIN)
    return render_template("idv/list.html", idvs=idvs, can_write=can_write(),
                           is_admin=is_admin,
                           org_units=org_units, persons_fv=persons_fv,
                           share_roots=share_roots)


# ── Globale Schnellsuche (JSON) ────────────────────────────────────────────

@bp.route("/api/quick-search")
@login_required
def quick_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    db = get_db()
    rows = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.status,
               r.idv_typ, ou.kuerzel AS oe_kuerzel
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        LEFT JOIN geschaeftsprozesse gp ON r.gp_id = gp.id
        WHERE r.status NOT IN ('Archiviert')
          AND (r.idv_id        LIKE ?
            OR r.bezeichnung   LIKE ?
            OR r.kurzbeschreibung LIKE ?
            OR gp.bezeichnung  LIKE ?
            OR r.gp_freitext   LIKE ?)
        ORDER BY r.idv_id
        LIMIT 12
    """, (f"%{q}%",) * 5).fetchall()
    return jsonify([
        {
            "id":       row["id"],
            "idv_id":   row["idv_id"],
            "name":     row["bezeichnung"],
            "status":   row["status"],
            "typ":      row["idv_typ"] or "",
            "oe":       row["oe_kuerzel"] or "",
            "url":      url_for("idv.detail_idv", idv_db_id=row["id"]),
        }
        for row in rows
    ])


# ── Bulk-Löschen (Admin) ───────────────────────────────────────────────────

@bp.route("/bulk-loeschen", methods=["POST"])
@admin_required
def bulk_loeschen():
    """Löscht mehrere IDVs auf einmal (nur IDV-Administrator)."""
    db        = get_db()
    person_id = session.get("person_id")
    raw_ids   = request.form.getlist("idv_ids")

    try:
        idv_db_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige IDV-IDs.", "error")
        return redirect(url_for("idv.list_idv"))

    if not idv_db_ids:
        flash("Keine IDVs ausgewählt.", "warning")
        return redirect(url_for("idv.list_idv"))

    deleted = 0
    for idv_db_id in idv_db_ids:
        row = db.execute("SELECT idv_id FROM idv_register WHERE id=?", (idv_db_id,)).fetchone()
        if not row:
            continue
        # Abhängige Datensätze ohne CASCADE zuerst löschen
        db.execute("DELETE FROM idv_history        WHERE idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM massnahmen          WHERE idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM pruefungen          WHERE idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM genehmigungen       WHERE idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM dokumente           WHERE idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM idv_abhaengigkeiten WHERE quell_idv_id = ?", (idv_db_id,))
        db.execute("DELETE FROM idv_abhaengigkeiten WHERE ziel_idv_id  = ?", (idv_db_id,))
        # Vorgänger-Verknüpfung in Nachfolgern aufheben
        db.execute("UPDATE idv_register SET vorgaenger_idv_id = NULL WHERE vorgaenger_idv_id = ?",
                   (idv_db_id,))
        # IDV löschen (CASCADE für idv_freigaben, idv_file_links, idv_wesentlichkeit)
        db.execute("DELETE FROM idv_register WHERE id=?", (idv_db_id,))
        deleted += 1

    db.commit()
    flash(f"{deleted} IDV(s) gelöscht.", "success")
    return redirect(url_for("idv.list_idv"))


# ── Bulk-Statusänderung (Admin + Koordinator) ─────────────────────────────

_BULK_STATUS_ERLAUBT = [
    "Entwurf", "In Prüfung", "Genehmigt", "Genehmigt mit Auflagen",
    "Abgelehnt", "Abgekündigt", "Archiviert",
]

@bp.route("/bulk-status", methods=["POST"])
@write_access_required
def bulk_status():
    """Setzt den Status mehrerer IDVs auf einmal (Admin + Koordinator)."""
    db        = get_db()
    person_id = session.get("person_id")
    raw_ids   = request.form.getlist("idv_ids")
    neuer_status = request.form.get("neuer_status", "").strip()

    if neuer_status not in _BULK_STATUS_ERLAUBT:
        flash("Bitte einen gültigen Zielstatus auswählen.", "error")
        return redirect(url_for("idv.list_idv"))

    try:
        idv_db_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige IDV-IDs.", "error")
        return redirect(url_for("idv.list_idv"))

    if not idv_db_ids:
        flash("Keine IDVs ausgewählt.", "warning")
        return redirect(url_for("idv.list_idv"))

    updated = errors = 0
    for idv_db_id in idv_db_ids:
        try:
            change_status(db, idv_db_id, neuer_status, geaendert_von_id=person_id)
            updated += 1
        except Exception:
            errors += 1

    msg = f'{updated} IDV(s) auf "{neuer_status}" gesetzt.'
    if errors:
        msg += f" {errors} konnten nicht geändert werden."
    flash(msg, "success" if not errors else "warning")
    return redirect(url_for("idv.list_idv"))


# ── Detail ─────────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>")
@login_required
def detail_idv(idv_db_id):
    db  = get_db()
    idv = db.execute("""
        SELECT r.*, v.*,
          p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
          p_en.nachname || ', ' || p_en.vorname AS entwickler,
          ou.bezeichnung AS org_einheit,
          gp.schutzbedarf_a, gp.schutzbedarf_c,
          gp.schutzbedarf_i, gp.schutzbedarf_n
        FROM idv_register r
        LEFT JOIN v_idv_uebersicht v ON v.idv_id = r.idv_id
        LEFT JOIN persons p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        LEFT JOIN geschaeftsprozesse gp ON r.gp_id = gp.id
        WHERE r.id = ?
    """, (idv_db_id,)).fetchone()

    if not idv:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

    file = db.execute("SELECT * FROM idv_files WHERE id = ?", (idv["file_id"],)).fetchone() if idv["file_id"] else None

    # Zusätzlich verknüpfte Dateien (idv_file_links)
    try:
        extra_files = db.execute("""
            SELECT f.*, lnk.id AS link_id, lnk.linked_at
            FROM idv_file_links lnk
            JOIN idv_files f ON f.id = lnk.file_id
            WHERE lnk.idv_db_id = ?
            ORDER BY lnk.linked_at
        """, (idv_db_id,)).fetchall()
    except Exception:
        extra_files = []

    history = db.execute("""
        SELECT h.*, p.nachname || ', ' || p.vorname AS person
        FROM idv_history h
        LEFT JOIN persons p ON h.durchgefuehrt_von_id = p.id
        WHERE h.idv_id = ?
        ORDER BY h.durchgefuehrt_am DESC
        LIMIT 20
    """, (idv_db_id,)).fetchall()

    massnahmen = db.execute("""
        SELECT m.*, p.nachname || ', ' || p.vorname AS verantwortlicher,
               CASE WHEN m.faellig_am < date('now') AND m.status IN ('Offen','In Bearbeitung')
                    THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        LEFT JOIN persons p ON m.verantwortlicher_id = p.id
        WHERE m.idv_id = ?
        ORDER BY m.faellig_am ASC
    """, (idv_db_id,)).fetchall()

    wesentlichkeit = get_idv_wesentlichkeit(db, idv_db_id)

    # Versionshistorie
    vorgaenger = None
    if idv["vorgaenger_idv_id"]:
        vorgaenger = db.execute(
            "SELECT id, idv_id, bezeichnung, version, status FROM idv_register WHERE id=?",
            (idv["vorgaenger_idv_id"],)
        ).fetchone()
    nachfolger = db.execute(
        """SELECT id, idv_id, bezeichnung, version, status,
                  letzte_aenderungsart, letzte_aenderungsbegruendung
           FROM idv_register WHERE vorgaenger_idv_id=?""",
        (idv_db_id,)
    ).fetchall()

    # Freigabe-Schritte
    try:
        freigaben = db.execute("""
            SELECT f.*,
                   p_b.nachname || ', ' || p_b.vorname AS beauftragt_von,
                   p_d.nachname || ', ' || p_d.vorname AS durchgefuehrt_von,
                   p_z.nachname || ', ' || p_z.vorname AS zugewiesen_an
            FROM idv_freigaben f
            LEFT JOIN persons p_b ON f.beauftragt_von_id    = p_b.id
            LEFT JOIN persons p_d ON f.durchgefuehrt_von_id = p_d.id
            LEFT JOIN persons p_z ON f.zugewiesen_an_id     = p_z.id
            WHERE f.idv_id = ?
            ORDER BY f.erstellt_am
        """, (idv_db_id,)).fetchall()
    except Exception:
        freigaben = []

    # Personen für Freigabeanforderer-Auswahl
    freigabe_persons = db.execute(
        "SELECT id, nachname, vorname, rolle FROM persons WHERE aktiv=1 ORDER BY nachname"
    ).fetchall()

    ist_wesentlich = bool(
        idv["steuerungsrelevant"] or idv["rechnungslegungsrelevant"] or idv["dora_kritisch_wichtig"]
        or any(k["erfuellt"] for k in wesentlichkeit)
    )

    # Phasenstatus für die Freigabe-Anzeige
    _PHASE_1 = ["Fachlicher Test", "Technischer Test"]
    _PHASE_2 = ["Fachliche Abnahme", "Technische Abnahme"]
    phase1_schritte   = [f for f in freigaben if f["schritt"] in _PHASE_1]
    phase2_schritte   = [f for f in freigaben if f["schritt"] in _PHASE_2]
    phase1_gestartet  = len(phase1_schritte) > 0
    phase1_bestanden  = (
        {f["schritt"] for f in phase1_schritte if f["status"] == "Bestanden"} == set(_PHASE_1)
    )
    phase2_gestartet  = len(phase2_schritte) > 0
    phase2_bestanden  = (
        {f["schritt"] for f in phase2_schritte if f["status"] == "Bestanden"} == set(_PHASE_2)
    )
    hat_offenen_schritt = any(f["status"] == "Ausstehend" for f in freigaben)

    return render_template("idv/detail.html",
        idv=idv, file=file, extra_files=extra_files, history=history, massnahmen=massnahmen,
        wesentlichkeit=wesentlichkeit,
        vorgaenger=vorgaenger, nachfolger=nachfolger,
        freigaben=freigaben, ist_wesentlich=ist_wesentlich,
        freigabe_persons=freigabe_persons,
        phase1_gestartet=phase1_gestartet, phase1_bestanden=phase1_bestanden,
        phase2_gestartet=phase2_gestartet, phase2_bestanden=phase2_bestanden,
        hat_offenen_schritt=hat_offenen_schritt,
        bearbeitungsstatus_werte=_BEARBEITUNGSSTATUS_WERTE,
        dokumentationsstatus_werte=_DOKUMENTATIONSSTATUS_WERTE,
        can_create=can_create())


# ── Neu ────────────────────────────────────────────────────────────────────

@bp.route("/neu", methods=["GET", "POST"])
@own_write_required
def new_idv():
    db = get_db()
    if request.method == "POST":
        data = _form_to_dict(request.form)
        file_id = _int_or_none(request.form.get("file_id"))
        if file_id:
            data["file_id"] = file_id
        person_id = session.get("person_id")
        try:
            new_id = create_idv(db, data, erfasser_id=person_id)
            _save_wesentlichkeit_from_form(db, new_id, request.form)
            # Zusätzliche Dateien aus idv_file_links speichern
            extra_raw = request.form.get("extra_file_ids", "")
            for part in extra_raw.split(","):
                extra_id = _int_or_none(part.strip())
                if extra_id and extra_id != file_id:
                    try:
                        db.execute(
                            "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                            (new_id, extra_id)
                        )
                        db.execute(
                            "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id=?",
                            (extra_id,)
                        )
                    except Exception:
                        pass
            if extra_raw.strip():
                db.commit()
            flash("IDV erfolgreich angelegt.", "success")
            if request.form.get("save_action") == "save_and_new":
                return redirect(url_for("idv.new_idv"))
            return redirect(url_for("idv.detail_idv", idv_db_id=new_id))
        except Exception as e:
            flash(f"Fehler beim Speichern: {e}", "error")

    # Optionales Vorausfüllen aus Scannerfund
    fund          = None
    prefill       = {}
    extra_fonds   = []
    file_id       = _int_or_none(request.args.get("file_id"))
    extra_file_ids = request.args.get("extra_file_ids", "")
    if file_id:
        fund = db.execute("SELECT * FROM idv_files WHERE id = ?", (file_id,)).fetchone()
        if fund:
            ext = (fund["extension"] or "").lower()
            typ = _EXT_TO_TYP.get(ext, "unklassifiziert")
            if ext in (".xlsx", ".xls", ".xltx") and fund["has_macros"]:
                typ = "Excel-Makro"
            name = fund["file_name"]
            if ext and name.lower().endswith(ext):
                name = name[:-len(ext)]
            prefill = {
                "bezeichnung":   name,
                "idv_typ":       typ,
                "file_id":       file_id,
                "extra_file_ids": extra_file_ids,
            }
        # Zusätzliche Dateien für Banner laden
        if extra_file_ids:
            extra_ids_parsed = [_int_or_none(x.strip()) for x in extra_file_ids.split(",") if x.strip()]
            extra_ids_parsed = [i for i in extra_ids_parsed if i and i != file_id]
            if extra_ids_parsed:
                ph = ",".join("?" * len(extra_ids_parsed))
                extra_fonds = db.execute(
                    f"SELECT * FROM idv_files WHERE id IN ({ph})", extra_ids_parsed
                ).fetchall()

    return render_template("idv/form.html", idv=None,
                           fund=fund, prefill=prefill,
                           extra_fonds=extra_fonds,
                           wesentlichkeit_antworten={},
                           can_write=can_write(),
                           **_form_lookups(db))


# ── Bearbeiten ─────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/bearbeiten", methods=["GET", "POST"])
@own_write_required
def edit_idv(idv_db_id):
    db  = get_db()
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))
    # Eingeschränkte Rollen dürfen nur IDVs bearbeiten, an denen sie beteiligt sind
    pid = current_person_id()
    if not can_write() and pid not in (
        idv["fachverantwortlicher_id"],
        idv["idv_entwickler_id"],
        idv["idv_koordinator_id"],
        idv["stellvertreter_id"],
    ):
        flash("Zugriff verweigert – Sie sind an dieser IDV nicht als Verantwortlicher erfasst.", "error")
        from flask import abort
        abort(403)

    if request.method == "POST":
        data = _form_to_dict(request.form)
        person_id = session.get("person_id")
        try:
            update_idv(db, idv_db_id, data, geaendert_von_id=person_id)
            _save_wesentlichkeit_from_form(db, idv_db_id, request.form)
            flash("IDV gespeichert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
        except Exception as e:
            flash(f"Fehler: {e}", "error")

    # Vorhandene Kriterium-Antworten für das Formular aufbereiten
    wesentlichkeit_antworten = {
        row["kriterium_id"]: dict(row)
        for row in get_idv_wesentlichkeit(db, idv_db_id)
    }
    return render_template("idv/form.html", idv=idv, fund=None, prefill={},
                           wesentlichkeit_antworten=wesentlichkeit_antworten,
                           can_write=can_write(),
                           **_form_lookups(db))


# ── Status ─────────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/status", methods=["POST"])
@write_access_required
def change_status_route(idv_db_id):
    db        = get_db()
    new_status = request.form.get("status")
    person_id  = session.get("person_id")
    if new_status:
        change_status(db, idv_db_id, new_status, geaendert_von_id=person_id)
        flash(f"Status geändert zu: {new_status}", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


_BEARBEITUNGSSTATUS_WERTE = [
    "Wertung ausstehend", "In Bearbeitung", "Freigabe ausstehend", "Freigegeben"
]
_DOKUMENTATIONSSTATUS_WERTE = [
    "Nicht dokumentiert", "In Dokumentation", "Dokumentiert"
]


@bp.route("/<int:idv_db_id>/bearbeitungsstatus", methods=["POST"])
@own_write_required
def change_bearbeitungsstatus(idv_db_id):
    db  = get_db()
    val = request.form.get("bearbeitungsstatus", "")
    if val not in _BEARBEITUNGSSTATUS_WERTE:
        flash("Ungültiger Bearbeitungsstatus.", "error")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
    person_id = session.get("person_id")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus=?, aktualisiert_am=? WHERE id=?",
        (val, now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "bearbeitungsstatus_geaendert", f"Bearbeitungsstatus → {val}", person_id)
    )
    db.commit()
    flash(f"Bearbeitungsstatus geändert zu: {val}", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:idv_db_id>/datei-verknuepfung/<int:link_id>/loeschen", methods=["POST"])
@own_write_required
def unlink_file(idv_db_id, link_id):
    """Entfernt eine zusätzliche Datei-Verknüpfung (idv_file_links)."""
    db = get_db()
    row = db.execute(
        "SELECT lnk.*, f.file_name FROM idv_file_links lnk JOIN idv_files f ON f.id=lnk.file_id WHERE lnk.id=? AND lnk.idv_db_id=?",
        (link_id, idv_db_id)
    ).fetchone()
    if not row:
        flash("Verknüpfung nicht gefunden.", "error")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
    db.execute("DELETE FROM idv_file_links WHERE id=?", (link_id,))
    db.execute(
        "UPDATE idv_files SET bearbeitungsstatus='Neu' WHERE id=? AND bearbeitungsstatus='Registriert'",
        (row["file_id"],)
    )
    db.commit()
    flash(f"Verknüpfung mit \"{row['file_name']}\" aufgehoben.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:idv_db_id>/dokumentationsstatus", methods=["POST"])
@own_write_required
def change_dokumentationsstatus(idv_db_id):
    db  = get_db()
    val = request.form.get("dokumentationsstatus", "")
    if val not in _DOKUMENTATIONSSTATUS_WERTE:
        flash("Ungültiger Dokumentationsstatus.", "error")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
    person_id = session.get("person_id")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE idv_register SET dokumentationsstatus=?, aktualisiert_am=? WHERE id=?",
        (val, now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "dokumentationsstatus_geaendert", f"Dokumentationsstatus → {val}", person_id)
    )
    db.commit()
    flash(f"Dokumentationsstatus geändert zu: {val}", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ── Neue Version ───────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/neue-version", methods=["POST"])
@own_write_required
def neue_version(idv_db_id):
    """Erstellt eine neue Version einer IDV (Nachfolger-Dokument)."""
    db  = get_db()
    src = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not src:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

    # Versionsnummer inkrementieren: "1.0" → "1.1", "1.9" → "1.10"
    version_str = src["version"] or "1.0"
    try:
        parts = version_str.split(".")
        major = parts[0]
        minor = int(parts[1]) + 1 if len(parts) > 1 else 1
        new_version = f"{major}.{minor}"
    except (ValueError, IndexError):
        new_version = version_str + ".1"

    # Änderungsart aus Formular
    aenderungsart      = request.form.get("aenderungsart", "").strip() or None
    aenderungsbegruendung = request.form.get("aenderungsbegruendung", "").strip() or None

    # Daten aus Quell-IDV kopieren
    data = dict(src)
    data["vorgaenger_idv_id"]             = idv_db_id
    data["version"]                       = new_version
    data["bearbeitungsstatus"]            = "Wertung ausstehend"
    data["dokumentationsstatus"]          = "Nicht dokumentiert"
    data["letzte_aenderungsart"]          = aenderungsart
    data["letzte_aenderungsbegruendung"]  = aenderungsbegruendung
    # Felder entfernen, die create_idv selbst setzt oder die versionsexklusiv sind.
    # file_id und weitere_dateien werden NICHT kopiert: jede Version muss ihre eigene
    # Datei explizit verknüpfen, damit die Dateihistorie je Version nachvollziehbar ist.
    for k in ("id", "idv_id", "status", "status_geaendert_am", "status_geaendert_von_id",
              "erstellt_am", "aktualisiert_am", "erfasst_von_id",
              "naechste_pruefung", "letzte_pruefung",
              "file_id", "weitere_dateien"):
        data.pop(k, None)

    person_id = session.get("person_id")
    try:
        new_id = create_idv(db, data, erfasser_id=person_id)

        # Wesentlichkeitskriterien-Antworten aus Quelle kopieren
        antworten = get_idv_wesentlichkeit(db, idv_db_id)
        from db import save_idv_wesentlichkeit
        save_idv_wesentlichkeit(db, new_id, [
            {"kriterium_id": a["kriterium_id"], "erfuellt": a["erfuellt"],
             "begruendung": a["begruendung"]}
            for a in antworten
        ])

        # History-Eintrag auf Quell-IDV
        new_idv_row = db.execute("SELECT idv_id FROM idv_register WHERE id=?", (new_id,)).fetchone()
        aenderung_info = ""
        if aenderungsart:
            aenderung_info = f" | Änderungsart: {aenderungsart}"
            if aenderungsbegruendung:
                aenderung_info += f" – {aenderungsbegruendung}"
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "neue_version",
             f"Nachfolger {new_idv_row['idv_id']} (v{new_version}) angelegt{aenderung_info}",
             person_id)
        )
        db.commit()

        flash(f"Neue Version {new_version} angelegt.", "success")
        return redirect(url_for("idv.detail_idv", idv_db_id=new_id))
    except Exception as e:
        flash(f"Fehler beim Anlegen der neuen Version: {e}", "error")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ── Excel-Export ───────────────────────────────────────────────────────────

@bp.route("/export/excel")
@login_required
def export_excel():
    """Erzeugt einen Excel-Export der aktuellen Grundgesamtheit."""
    import tempfile, subprocess, sys
    from flask import current_app
    db_path = current_app.config["DATABASE"]
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        out_path = f.name
    try:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                               "scanner", "idv_export.py")
        subprocess.run([sys.executable, script, "--db", db_path, "--output", out_path], check=True)
        return send_file(out_path, as_attachment=True,
                         download_name="IDV_Grundgesamtheit.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        flash(f"Export fehlgeschlagen: {e}", "error")
        return redirect(url_for("idv.list_idv"))


# ── Hilfsfunktion: Formular → Dict ─────────────────────────────────────────

def _form_to_dict(form) -> dict:
    def chk(k): return 1 if form.get(k) == "1" else 0

    return {
        "bezeichnung":               form.get("bezeichnung", "").strip(),
        "kurzbeschreibung":          form.get("kurzbeschreibung", "").strip() or None,
        "version":                   form.get("version", "1.0").strip(),
        "idv_typ":                   form.get("idv_typ", "unklassifiziert"),
        "steuerungsrelevant":        chk("steuerungsrelevant"),
        "steuerungsrelevanz_begr":   form.get("steuerungsrelevanz_begr") or None,
        "rechnungslegungsrelevant":  chk("rechnungslegungsrelevant"),
        "rechnungslegungsrelevanz_begr": form.get("rechnungslegungsrelevanz_begr") or None,
        "gda_wert":                  _int_or_none(form.get("gda_wert")) or 1,
        "gp_id":                     _int_or_none(form.get("gp_id")),
        "gp_freitext":               form.get("gp_freitext") or None,
        "dora_kritisch_wichtig":     chk("dora_kritisch_wichtig"),
        "dora_begruendung":          form.get("dora_begruendung") or None,
        "risikoklasse_id":           _int_or_none(form.get("risikoklasse_id")),
        "org_unit_id":               _int_or_none(form.get("org_unit_id")),
        "fachverantwortlicher_id":   _int_or_none(form.get("fachverantwortlicher_id")),
        "idv_entwickler_id":         _int_or_none(form.get("idv_entwickler_id")),
        "idv_koordinator_id":        _int_or_none(form.get("idv_koordinator_id")),
        "stellvertreter_id":         _int_or_none(form.get("stellvertreter_id")),
        "plattform_id":              _int_or_none(form.get("plattform_id")),
        "programmiersprache":        form.get("programmiersprache") or None,
        "datenbankanbindung":        chk("datenbankanbindung"),
        "datenbankanbindung_beschr": form.get("datenbankanbindung_beschr") or None,
        "netzwerkzugriff":           chk("netzwerkzugriff"),
        "enthaelt_personendaten":    chk("enthaelt_personendaten"),
        "datenschutz_kategorie":     form.get("datenschutz_kategorie") or "keine",
        "produktiv_seit":            form.get("produktiv_seit") or None,
        "nutzungsfrequenz":          form.get("nutzungsfrequenz") or None,
        "nutzeranzahl":              _int_or_none(form.get("nutzeranzahl")),
        "datenquellen":              form.get("datenquellen") or None,
        "datenempfaenger":           form.get("datenempfaenger") or None,
        "dokumentation_vorhanden":   chk("dokumentation_vorhanden"),
        "testkonzept_vorhanden":     chk("testkonzept_vorhanden"),
        "versionskontrolle":         chk("versionskontrolle"),
        "zugriffsschutz":            chk("zugriffsschutz"),
        "vier_augen_prinzip":        chk("vier_augen_prinzip"),
        "pruefintervall_monate":     _int_or_none(form.get("pruefintervall_monate")) or 12,
        "abloesung_geplant":         chk("abloesung_geplant"),
        "abloesung_zieldatum":       form.get("abloesung_zieldatum") or None,
        "abloesung_durch":           form.get("abloesung_durch") or None,
        "interne_notizen":           form.get("interne_notizen") or None,
        # Neue Felder (außerhalb Wesentlichkeitsbeurteilung)
        "gobd_relevant":             chk("gobd_relevant"),
        "erstellt_fuer":             form.get("erstellt_fuer") or None,
        "schnittstellen_beschr":     form.get("schnittstellen_beschr") or None,
    }


def _save_wesentlichkeit_from_form(db, idv_db_id: int, form) -> None:
    """Liest die konfigurierbaren Kriterium-Antworten aus dem Formular und speichert sie."""
    criteria = db.execute(
        "SELECT id FROM wesentlichkeitskriterien WHERE aktiv=1"
    ).fetchall()
    antworten = []
    for k in criteria:
        kid = k["id"]
        antworten.append({
            "kriterium_id": kid,
            "erfuellt":     1 if form.get(f"kriterium_{kid}") == "1" else 0,
            "begruendung":  form.get(f"kriterium_begr_{kid}") or None,
        })
    if antworten:
        save_idv_wesentlichkeit(db, idv_db_id, antworten)
