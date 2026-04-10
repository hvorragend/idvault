from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
from . import login_required, write_access_required, get_db, can_write, can_read_all, current_person_id
import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import create_idv, update_idv, change_status, search_idv

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
        "org_units":        db.execute("SELECT * FROM org_units WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "persons":          db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall(),
        "geschaeftsprozesse": db.execute("SELECT * FROM geschaeftsprozesse WHERE aktiv=1 ORDER BY gp_nummer").fetchall(),
        "plattformen":      db.execute("SELECT * FROM plattformen WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "risikoklassen":    db.execute("SELECT * FROM risikoklassen ORDER BY sort_order").fetchall(),
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
    gda_min = _int_or_none(request.args.get("gda_min", "0")) or 0
    filt    = request.args.get("filter", "")

    # Alle Bedingungen und Parameter als positionale Listen aufbauen
    where_parts = []
    params      = []

    if q:
        where_parts.append("(v.idv_id LIKE ? OR v.bezeichnung LIKE ? OR v.geschaeftsprozess LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if status:
        where_parts.append("v.status = ?")
        params.append(status)
    if gda_min:
        where_parts.append("v.gda_wert >= ?")
        params.append(gda_min)

    # Spezialfilter
    if filt == "kritisch":
        where_parts.append("(v.gda_wert = 4 OR v.steuerungsrelevant = 'Ja' OR v.dora_kritisch = 'Ja')")
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

    # Rollenbasierte Sichtbarkeit:
    # Admin / Koordinator / Revision / IT-Sicherheit → alle IDVs
    # Fachverantwortlicher / Entwickler → nur eigene
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
        SELECT r.*, v.*
        FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        {where_sql}
        ORDER BY v.gda_wert DESC, v.bezeichnung
    """
    idvs = db.execute(sql, params).fetchall()
    return render_template("idv/list.html", idvs=idvs, can_write=can_write())


# ── Detail ─────────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>")
@login_required
def detail_idv(idv_db_id):
    db  = get_db()
    idv = db.execute("""
        SELECT r.*, v.*,
          p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
          p_en.nachname || ', ' || p_en.vorname AS entwickler,
          ou.bezeichnung AS org_einheit
        FROM idv_register r
        LEFT JOIN v_idv_uebersicht v ON v.idv_id = r.idv_id
        LEFT JOIN persons p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.id = ?
    """, (idv_db_id,)).fetchone()

    if not idv:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

    file = db.execute("SELECT * FROM idv_files WHERE id = ?", (idv["file_id"],)).fetchone() if idv["file_id"] else None

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

    return render_template("idv/detail.html",
        idv=idv, file=file, history=history, massnahmen=massnahmen)


# ── Neu ────────────────────────────────────────────────────────────────────

@bp.route("/neu", methods=["GET", "POST"])
@write_access_required
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
            flash("IDV erfolgreich angelegt.", "success")
            if request.form.get("save_action") == "save_and_new":
                return redirect(url_for("idv.new_idv"))
            return redirect(url_for("idv.detail_idv", idv_db_id=new_id))
        except Exception as e:
            flash(f"Fehler beim Speichern: {e}", "error")

    # Optionales Vorausfüllen aus Scannerfund
    fund    = None
    prefill = {}
    file_id = _int_or_none(request.args.get("file_id"))
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
                "bezeichnung": name,
                "idv_typ":     typ,
                "file_id":     file_id,
            }

    return render_template("idv/form.html", idv=None,
                           fund=fund, prefill=prefill, **_form_lookups(db))


# ── Bearbeiten ─────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/bearbeiten", methods=["GET", "POST"])
@write_access_required
def edit_idv(idv_db_id):
    db  = get_db()
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("IDV nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

    if request.method == "POST":
        data = _form_to_dict(request.form)
        person_id = session.get("person_id")
        try:
            update_idv(db, idv_db_id, data, geaendert_von_id=person_id)
            flash("IDV gespeichert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
        except Exception as e:
            flash(f"Fehler: {e}", "error")

    return render_template("idv/form.html", idv=idv, fund=None, prefill={}, **_form_lookups(db))


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
        "relevant_guv":              chk("relevant_guv"),
        "relevant_meldewesen":       chk("relevant_meldewesen"),
        "relevant_risikomanagement": chk("relevant_risikomanagement"),
        "rechnungslegungsrelevant":  chk("rechnungslegungsrelevant"),
        "rechnungslegungsrelevanz_begr": form.get("rechnungslegungsrelevanz_begr") or None,
        "gda_wert":                  _int_or_none(form.get("gda_wert")) or 1,
        "gda_begruendung":           form.get("gda_begruendung") or None,
        "gp_id":                     _int_or_none(form.get("gp_id")),
        "gp_freitext":               form.get("gp_freitext") or None,
        "dora_kritisch_wichtig":     chk("dora_kritisch_wichtig"),
        "dora_begruendung":          form.get("dora_begruendung") or None,
        "risikoklasse_id":           _int_or_none(form.get("risikoklasse_id")),
        "risiko_verfuegbarkeit":     _int_or_none(form.get("risiko_verfuegbarkeit")),
        "risiko_integritaet":        _int_or_none(form.get("risiko_integritaet")),
        "risiko_vertraulichkeit":    _int_or_none(form.get("risiko_vertraulichkeit")),
        "risiko_nachvollziehbarkeit":_int_or_none(form.get("risiko_nachvollziehbarkeit")),
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
    }
