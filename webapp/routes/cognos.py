"""Cognos-Berichte-Blueprint – Berichtsübersicht-Import aus agree21Analysen."""

import calendar
import csv
import io
import os
import sys
from datetime import datetime, date, timezone

from flask import (
    Blueprint, render_template, request, flash, redirect, url_for, current_app
)
from . import login_required, write_access_required, get_db, can_write, current_person_id

# db.py liegt zwei Ebenen über webapp/routes/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import generate_idv_id  # noqa: E402

bp = Blueprint("cognos", __name__, url_prefix="/cognos")

# ---------------------------------------------------------------------------
# TSV-Spalten-Mapping  (Spaltenköpfe aus Berichtsübersicht → DB-Feldnamen)
# ---------------------------------------------------------------------------

_COLUMN_MAP = {
    "Umfeld":                                          "umfeld",
    "Bank-ID":                                         "bank_id",
    "Anwendung":                                       "anwendung",
    "Berichtsname":                                    "berichtsname",
    "Suchpfad":                                        "suchpfad",
    "Package":                                         "package",
    "Eigentümer":                                      "eigentuemer",
    "Berichtsbeschreibung":                            "berichtsbeschreibung",
    "Erstelldatum":                                    "erstelldatum",
    "Änderungsdatum":                                  "aenderungsdatum",
    "Letztes Ausführungsdatum (nur Hintergrund)":      "letztes_ausfuehrungsdatum",
    "Letzter Ausführungsstatus (nur Hintergrund)":     "letzter_ausfuehrungsstatus",
    "Anz. Abfragen":                                   "anz_abfragen",
    "Anz. Datenelemente":                              "anz_datenelemente",
    "Anz. Felder/Klarnamen":                           "anz_felder_klarnamen",
    "Anz. Filter":                                     "anz_filter",
    "Summe Ausdruckslänge":                            "summe_ausdruckslaenge",
    "Komplexität [0-10]":                              "komplexitaet",
    "Datum Berichtsabzug":                             "datum_berichtsabzug",
}

_INT_COLS   = {"anz_abfragen", "anz_datenelemente", "anz_felder_klarnamen",
               "anz_filter", "summe_ausdruckslaenge"}
_FLOAT_COLS = {"komplexitaet"}


def _parse_german_number(value: str, as_float: bool = False):
    """Konvertiert deutsche Zahlformate: '1.057' → 1057, '5,2' → 5.2"""
    if not value or not value.strip():
        return None
    v = value.strip().replace(".", "").replace(",", ".")
    try:
        return float(v) if as_float else int(v)
    except ValueError:
        return None


def _parse_tsv(file_bytes: bytes, filename: str) -> tuple:
    """
    Parst die Berichtsübersicht-TSV-Datei.

    Rückgabe: (rows: list[dict], fehler: list[str])
    """
    rows   = []
    fehler = []

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return [], ["Datei konnte nicht dekodiert werden (kein UTF-8 / Latin-1)."]

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    lines  = list(reader)

    if not lines:
        return [], ["Datei ist leer."]

    # Erste Zeile: ggf. Titelzeile überspringen
    start = 0
    first = lines[0][0].strip() if lines[0] else ""
    if first.startswith("Berichtsübersicht") or first.startswith("Berichts\u00fcbersicht"):
        start = 1

    if start >= len(lines):
        return [], ["Keine Spaltenköpfe gefunden."]

    headers = [h.strip() for h in lines[start]]
    start  += 1  # ab jetzt: Datenzeilen

    # Spalten mappen
    col_idx = {}
    for h, db_col in _COLUMN_MAP.items():
        if h in headers:
            col_idx[db_col] = headers.index(h)

    if "berichtsname" not in col_idx:
        return [], ["Pflichtfeld 'Berichtsname' nicht in der Datei gefunden. "
                    "Bitte Spaltenköpfe prüfen."]

    for lnum, line in enumerate(lines[start:], start=start + 2):
        if not any(c.strip() for c in line):
            continue  # Leerzeile überspringen
        row = {}
        for db_col, idx in col_idx.items():
            raw = line[idx].strip() if idx < len(line) else ""
            if db_col in _INT_COLS:
                row[db_col] = _parse_german_number(raw, as_float=False)
            elif db_col in _FLOAT_COLS:
                row[db_col] = _parse_german_number(raw, as_float=True)
            else:
                row[db_col] = raw or None
        if not row.get("berichtsname"):
            fehler.append(f"Zeile {lnum}: leerer Berichtsname – übersprungen.")
            continue
        rows.append(row)

    return rows, fehler


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

_VALID_PER_PAGE = (25, 50, 100, 200, 500)


@bp.route("/")
@login_required
def list_berichte():
    db = get_db()

    anwendung_filt = request.args.get("anwendung", "").strip()
    package_filt   = request.args.get("package",   "").strip()
    status_filt    = request.args.get("status",    "").strip()
    bank_id_filt   = request.args.get("bank_id",   "").strip()
    q              = request.args.get("q",          "").strip()

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except (ValueError, TypeError):
        per_page = 50
    if per_page not in _VALID_PER_PAGE:
        per_page = 50

    where_parts = []
    params      = []

    if anwendung_filt:
        where_parts.append("anwendung = ?")
        params.append(anwendung_filt)
    if package_filt:
        where_parts.append("package = ?")
        params.append(package_filt)
    if status_filt:
        where_parts.append("bearbeitungsstatus = ?")
        params.append(status_filt)
    if bank_id_filt:
        where_parts.append("bank_id = ?")
        params.append(bank_id_filt)
    if q:
        where_parts.append("(berichtsname LIKE ? OR suchpfad LIKE ? OR eigentuemer LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = db.execute(
        f"SELECT COUNT(*) FROM cognos_berichte {where_sql}", params
    ).fetchone()[0]

    offset   = (page - 1) * per_page
    berichte = db.execute(
        f"""SELECT cb.*,
                   r.idv_id AS idv_id_str
            FROM cognos_berichte cb
            LEFT JOIN idv_register r ON r.id = cb.idv_register_id
            {where_sql}
            ORDER BY cb.berichtsname
            LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    # Filter-Werte für Dropdowns
    anwendungen = [r[0] for r in db.execute(
        "SELECT DISTINCT anwendung FROM cognos_berichte WHERE anwendung IS NOT NULL ORDER BY anwendung"
    ).fetchall()]
    packages = [r[0] for r in db.execute(
        "SELECT DISTINCT package FROM cognos_berichte WHERE package IS NOT NULL ORDER BY package"
    ).fetchall()]

    stats = db.execute("""
        SELECT
            COUNT(*) AS gesamt,
            SUM(CASE WHEN bearbeitungsstatus='Neu'         THEN 1 ELSE 0 END) AS neu,
            SUM(CASE WHEN bearbeitungsstatus='Registriert' THEN 1 ELSE 0 END) AS registriert,
            SUM(CASE WHEN bearbeitungsstatus='Ignoriert'   THEN 1 ELSE 0 END) AS ignoriert
        FROM cognos_berichte
    """).fetchone()

    return render_template(
        "cognos/list.html",
        berichte=berichte,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        anwendungen=anwendungen,
        packages=packages,
        anwendung_filt=anwendung_filt,
        package_filt=package_filt,
        status_filt=status_filt,
        bank_id_filt=bank_id_filt,
        q=q,
        stats=stats,
        can_write=can_write(),
    )


@bp.route("/import", methods=["GET", "POST"])
@login_required
@write_access_required
def import_berichte():
    if request.method == "GET":
        return render_template("cognos/import.html")

    uploaded = request.files.get("berichtsuebersicht")
    if not uploaded or not uploaded.filename:
        flash("Keine Datei ausgewählt.", "error")
        return render_template("cognos/import.html")

    file_bytes = uploaded.read()
    filename   = uploaded.filename

    rows, fehler = _parse_tsv(file_bytes, filename)

    if not rows and fehler:
        for err in fehler:
            flash(err, "error")
        return render_template("cognos/import.html")

    db          = get_db()
    now         = datetime.now(timezone.utc).isoformat()
    person_id   = current_person_id()

    eingefuegt  = 0
    aktualisiert = 0

    for row in rows:
        # Prüfen ob bereits vorhanden (nach Unique-Key)
        bank_id     = row.get("bank_id") or ""
        berichtsname = row.get("berichtsname") or ""
        suchpfad    = row.get("suchpfad") or ""

        existing = db.execute(
            "SELECT id, bearbeitungsstatus FROM cognos_berichte "
            "WHERE bank_id=? AND berichtsname=? AND suchpfad=?",
            (bank_id, berichtsname, suchpfad)
        ).fetchone()

        if existing:
            # Nur Metadaten aktualisieren, bearbeitungsstatus erhalten
            db.execute("""
                UPDATE cognos_berichte SET
                    import_datei_name=?, importiert_am=?, importiert_von_id=?,
                    umfeld=?, anwendung=?, package=?, eigentuemer=?,
                    berichtsbeschreibung=?,
                    erstelldatum=?, aenderungsdatum=?,
                    letztes_ausfuehrungsdatum=?, letzter_ausfuehrungsstatus=?,
                    anz_abfragen=?, anz_datenelemente=?, anz_felder_klarnamen=?,
                    anz_filter=?, summe_ausdruckslaenge=?, komplexitaet=?,
                    datum_berichtsabzug=?
                WHERE id=?
            """, (
                filename, now, person_id,
                row.get("umfeld"), row.get("anwendung"), row.get("package"),
                row.get("eigentuemer"), row.get("berichtsbeschreibung"),
                row.get("erstelldatum"), row.get("aenderungsdatum"),
                row.get("letztes_ausfuehrungsdatum"), row.get("letzter_ausfuehrungsstatus"),
                row.get("anz_abfragen"), row.get("anz_datenelemente"),
                row.get("anz_felder_klarnamen"), row.get("anz_filter"),
                row.get("summe_ausdruckslaenge"), row.get("komplexitaet"),
                row.get("datum_berichtsabzug"),
                existing["id"],
            ))
            aktualisiert += 1
        else:
            db.execute("""
                INSERT INTO cognos_berichte (
                    import_datei_name, importiert_am, importiert_von_id,
                    umfeld, bank_id, anwendung, berichtsname, suchpfad,
                    package, eigentuemer, berichtsbeschreibung,
                    erstelldatum, aenderungsdatum,
                    letztes_ausfuehrungsdatum, letzter_ausfuehrungsstatus,
                    anz_abfragen, anz_datenelemente, anz_felder_klarnamen,
                    anz_filter, summe_ausdruckslaenge, komplexitaet,
                    datum_berichtsabzug
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                filename, now, person_id,
                row.get("umfeld"), bank_id, row.get("anwendung"),
                berichtsname, suchpfad,
                row.get("package"), row.get("eigentuemer"),
                row.get("berichtsbeschreibung"),
                row.get("erstelldatum"), row.get("aenderungsdatum"),
                row.get("letztes_ausfuehrungsdatum"),
                row.get("letzter_ausfuehrungsstatus"),
                row.get("anz_abfragen"), row.get("anz_datenelemente"),
                row.get("anz_felder_klarnamen"), row.get("anz_filter"),
                row.get("summe_ausdruckslaenge"), row.get("komplexitaet"),
                row.get("datum_berichtsabzug"),
            ))
            eingefuegt += 1

    db.commit()

    for err in fehler:
        flash(err, "warning")

    flash(
        f"Import abgeschlossen: {eingefuegt} neu importiert, "
        f"{aktualisiert} aktualisiert"
        + (f", {len(fehler)} Warnungen" if fehler else "") + ".",
        "success",
    )
    return redirect(url_for("cognos.list_berichte"))


@bp.route("/<int:bericht_id>/als-idv", methods=["POST"])
@login_required
@write_access_required
def als_idv_registrieren(bericht_id: int):
    db = get_db()
    bericht = db.execute(
        "SELECT * FROM cognos_berichte WHERE id=?", (bericht_id,)
    ).fetchone()
    if not bericht:
        flash("Bericht nicht gefunden.", "error")
        return redirect(url_for("cognos.list_berichte"))

    if bericht["idv_register_id"]:
        flash("Dieser Bericht ist bereits als IDV registriert.", "warning")
        return redirect(url_for("idv.detail_idv", id=bericht["idv_register_id"]))

    now    = datetime.now(timezone.utc).isoformat()
    idv_id = generate_idv_id(db)

    # Nächste Prüfung: +12 Monate
    today     = date.today()
    np_month  = today.month - 1 + 12
    np_year   = today.year + np_month // 12
    np_month  = np_month % 12 + 1
    naechste_pruefung = date(
        np_year, np_month,
        min(today.day, calendar.monthrange(np_year, np_month)[1])
    ).isoformat()

    cur = db.execute("""
        INSERT INTO idv_register (
            idv_id, bezeichnung, kurzbeschreibung, idv_typ,
            gda_wert, steuerungsrelevant, rechnungslegungsrelevant,
            dora_kritisch_wichtig, enthaelt_personendaten,
            pruefintervall_monate, naechste_pruefung,
            status, erstellt_am, aktualisiert_am, teststatus
        ) VALUES (
            ?, ?, ?, 'Cognos-Report',
            1, 0, 0,
            0, 0,
            12, ?,
            'Entwurf', ?, ?, 'Wertung ausstehend'
        )
    """, (
        idv_id,
        bericht["berichtsname"],
        bericht["suchpfad"],
        naechste_pruefung,
        now, now,
    ))
    new_id = cur.lastrowid

    db.execute(
        "UPDATE cognos_berichte SET idv_register_id=?, bearbeitungsstatus='Registriert' WHERE id=?",
        (new_id, bericht_id),
    )
    db.commit()

    flash(f"IDV {idv_id} wurde angelegt. Bitte jetzt vervollständigen.", "success")
    return redirect(url_for("idv.detail_idv", id=new_id))


@bp.route("/<int:bericht_id>/ignorieren", methods=["POST"])
@login_required
@write_access_required
def ignorieren(bericht_id: int):
    db = get_db()
    db.execute(
        "UPDATE cognos_berichte SET bearbeitungsstatus='Ignoriert' WHERE id=?",
        (bericht_id,),
    )
    db.commit()
    flash("Bericht wurde als ignoriert markiert.", "info")
    return redirect(request.referrer or url_for("cognos.list_berichte"))


@bp.route("/<int:bericht_id>/reaktivieren", methods=["POST"])
@login_required
@write_access_required
def reaktivieren(bericht_id: int):
    db = get_db()
    db.execute(
        "UPDATE cognos_berichte SET bearbeitungsstatus='Neu' WHERE id=? AND bearbeitungsstatus='Ignoriert'",
        (bericht_id,),
    )
    db.commit()
    flash("Bericht reaktiviert.", "info")
    return redirect(request.referrer or url_for("cognos.list_berichte"))
