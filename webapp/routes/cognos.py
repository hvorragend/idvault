"""Cognos-Berichte-Blueprint – Berichtsübersicht-Import aus agree21Analysen."""

import calendar
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


def _coerce_cell(raw, db_col):
    """Wandelt einen Rohwert (aus Excel oder TSV) in den passenden Python-Typ um."""
    if db_col in _INT_COLS:
        if isinstance(raw, (int, float)):
            return int(raw)
        return _parse_german_number(str(raw), as_float=False) if raw else None
    if db_col in _FLOAT_COLS:
        if isinstance(raw, (int, float)):
            return float(raw)
        return _parse_german_number(str(raw), as_float=True) if raw else None
    # Text-Felder: None oder getrimmter String
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _rows_from_table(header_row, data_rows, offset: int = 2) -> tuple:
    """Gemeinsame Spaltenzuordnung und Zeilenverarbeitung für Excel und TSV."""
    fehler = []
    rows   = []

    col_idx = {}
    for h, db_col in _COLUMN_MAP.items():
        if h in header_row:
            col_idx[db_col] = header_row.index(h)

    if "berichtsname" not in col_idx:
        return [], ["Pflichtfeld 'Berichtsname' nicht gefunden. Bitte Spaltenköpfe prüfen."]

    for lnum, line in enumerate(data_rows, start=offset):
        if not any(v for v in line if v is not None and str(v).strip()):
            continue  # Leerzeile
        row = {
            db_col: _coerce_cell(
                line[idx] if idx < len(line) else None, db_col
            )
            for db_col, idx in col_idx.items()
        }
        if not row.get("berichtsname"):
            fehler.append(f"Zeile {lnum}: leerer Berichtsname – übersprungen.")
            continue
        rows.append(row)

    return rows, fehler


def _parse_file(file_bytes: bytes, filename: str) -> tuple:
    """Parst die Excel-Berichtsübersicht (.xlsx / .xlsm).

    Festes Layout:
      Zeile 1–2: Titel / Metadaten  (werden übersprungen)
      Zeile 3:   Spaltenköpfe
      Zeile 4+:  Datensätze
    """
    try:
        import openpyxl
    except ImportError:
        return [], ["openpyxl ist nicht verfügbar."]

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as exc:
        return [], [f"Excel-Datei konnte nicht geöffnet werden: {exc}"]

    # Zeile 3 = Index 2 = Spaltenköpfe; Zeile 4+ = Daten
    if len(all_rows) < 3:
        return [], ["Excel-Datei hat weniger als 3 Zeilen – Spaltenköpfe nicht gefunden."]

    headers   = [str(c or "").strip() for c in all_rows[2]]   # Zeile 3
    data_rows = [list(r) for r in all_rows[3:]]               # ab Zeile 4

    return _rows_from_table(headers, data_rows, offset=4)


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

_VALID_PER_PAGE = (25, 50, 100, 200, 500, 1000, 2000)


_SORT_COLS = {
    "berichtsname":         "cb.berichtsname",
    "suchpfad":             "cb.suchpfad",
    "package":              "cb.package",
    "eigentuemer":          "cb.eigentuemer",
    "anz_abfragen":         "cb.anz_abfragen",
    "anz_datenelemente":    "cb.anz_datenelemente",
    "anz_felder_klarnamen": "cb.anz_felder_klarnamen",
    "anz_filter":           "cb.anz_filter",
    "summe_ausdruckslaenge":"cb.summe_ausdruckslaenge",
    "komplexitaet":         "cb.komplexitaet",
    "status":               "cb.bearbeitungsstatus",
}


@bp.route("/")
@login_required
def list_berichte():
    db = get_db()
    from flask import session as _session

    status_filt       = request.args.get("status",       "").strip()
    komplexitaet_filt = request.args.get("komplexitaet", "").strip()
    abfragen_filt     = request.args.get("abfragen",     "").strip()
    pfad_prefix       = request.args.get("pfad_prefix",  "").strip()
    q                 = request.args.get("q",             "").strip()
    sort              = request.args.get("sort",  "berichtsname").strip()
    order             = request.args.get("order", "asc").strip()

    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    # per_page: explicit URL param > session preference > default 50
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 50
        if per_page in _VALID_PER_PAGE:
            _session["pref_per_page_cognos"] = per_page
    else:
        per_page = _session.get("pref_per_page_cognos", 50)
    if per_page not in _VALID_PER_PAGE:
        per_page = 50

    sort_col = _SORT_COLS.get(sort, "cb.berichtsname")
    sort_dir = "DESC" if order == "desc" else "ASC"

    where_parts = []
    params      = []

    if status_filt:
        where_parts.append("bearbeitungsstatus = ?")
        params.append(status_filt)
    if komplexitaet_filt == "niedrig":
        where_parts.append("komplexitaet IS NOT NULL AND komplexitaet <= 3")
    elif komplexitaet_filt == "mittel":
        where_parts.append("komplexitaet IS NOT NULL AND komplexitaet >= 4 AND komplexitaet <= 6")
    elif komplexitaet_filt == "hoch":
        where_parts.append("komplexitaet IS NOT NULL AND komplexitaet >= 7")
    if abfragen_filt == "1-5":
        where_parts.append("anz_abfragen IS NOT NULL AND anz_abfragen >= 1 AND anz_abfragen <= 5")
    elif abfragen_filt == "6-10":
        where_parts.append("anz_abfragen IS NOT NULL AND anz_abfragen >= 6 AND anz_abfragen <= 10")
    elif abfragen_filt == "11-20":
        where_parts.append("anz_abfragen IS NOT NULL AND anz_abfragen >= 11 AND anz_abfragen <= 20")
    elif abfragen_filt == "21+":
        where_parts.append("anz_abfragen IS NOT NULL AND anz_abfragen > 20")
    if pfad_prefix:
        where_parts.append("(suchpfad = ? OR suchpfad LIKE ?)")
        params.extend([pfad_prefix, pfad_prefix + " /%"])
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
            ORDER BY {sort_col} {sort_dir}
            LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    stats = db.execute("""
        SELECT
            COUNT(*) AS gesamt,
            SUM(CASE WHEN bearbeitungsstatus='Neu'         THEN 1 ELSE 0 END) AS neu,
            SUM(CASE WHEN bearbeitungsstatus='Registriert' THEN 1 ELSE 0 END) AS registriert,
            SUM(CASE WHEN bearbeitungsstatus='Ignoriert'   THEN 1 ELSE 0 END) AS ignoriert
        FROM cognos_berichte
    """).fetchone()

    persons = db.execute(
        "SELECT kuerzel, vorname, nachname FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    # Gemeinsamen Pfad-Präfix über ALLE Einträge der DB ermitteln (nicht nur aktuelle Seite)
    common_prefix = ""
    all_paths = [r[0] for r in db.execute(
        "SELECT DISTINCT suchpfad FROM cognos_berichte WHERE suchpfad IS NOT NULL"
    ).fetchall()]
    if len(all_paths) >= 2:
        split_paths = [p.split(" / ") for p in all_paths]
        min_len = min(len(p) for p in split_paths)
        depth = 0
        for i in range(min_len):
            if len({p[i] for p in split_paths}) == 1:
                depth += 1
            else:
                break
        if depth:
            common_prefix = " / ".join(split_paths[0][:depth])
    elif len(all_paths) == 1:
        # Nur ein Pfad: zeige alles ab Ebene 2 an (erste Ebene als Prefix)
        parts = all_paths[0].split(" / ")
        if len(parts) > 1:
            common_prefix = parts[0]

    # Eigentuemer → Person-Lookup (kuerzel, ad_name, user_id, oder "vorname nachname")
    eigentuemer_vals = {b["eigentuemer"] for b in berichte if b["eigentuemer"]}
    eigentuemer_map: dict[str, str] = {}
    for ev in eigentuemer_vals:
        row = db.execute(
            """SELECT vorname, nachname, kuerzel FROM persons
               WHERE aktiv=1 AND (
                   kuerzel=? OR ad_name=? OR user_id=?
                   OR (vorname || ' ' || nachname)=?
                   OR (nachname || ', ' || vorname)=?
                   OR (nachname || ' ' || vorname)=?
               ) LIMIT 1""",
            (ev, ev, ev, ev, ev, ev)
        ).fetchone()
        if row:
            eigentuemer_map[ev] = f"{row['nachname']}, {row['vorname']} ({row['kuerzel']})"

    return render_template(
        "cognos/list.html",
        berichte=berichte,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        valid_per_page=_VALID_PER_PAGE,
        status_filt=status_filt,
        komplexitaet_filt=komplexitaet_filt,
        abfragen_filt=abfragen_filt,
        pfad_prefix=pfad_prefix,
        q=q,
        sort=sort,
        order=order,
        stats=stats,
        can_write=can_write(),
        persons=persons,
        eigentuemer_map=eigentuemer_map,
        common_prefix=common_prefix,
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

    rows, fehler = _parse_file(file_bytes, filename)

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
        return redirect(url_for("idv.detail_idv", idv_db_id=bericht["idv_register_id"]))

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
    return redirect(url_for("idv.detail_idv", idv_db_id=new_id))


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


@bp.route("/<int:bericht_id>/loeschen", methods=["POST"])
@login_required
@write_access_required
def loeschen(bericht_id: int):
    db = get_db()
    db.execute("DELETE FROM cognos_berichte WHERE id=?", (bericht_id,))
    db.commit()
    flash("Bericht gelöscht.", "info")
    return redirect(request.referrer or url_for("cognos.list_berichte"))


@bp.route("/zusammenfassen", methods=["GET", "POST"])
@login_required
@write_access_required
def zusammenfassen():
    """Mehrere Cognos-Berichte zu einem IDV-Projekt zusammenfassen."""
    db = get_db()

    if request.method == "POST":
        aktion   = request.form.get("aktion", "")
        raw_ids  = request.form.getlist("bericht_ids")
        try:
            bericht_ids = [int(i) for i in raw_ids if i]
        except ValueError:
            flash("Ungültige IDs.", "error")
            return redirect(url_for("cognos.list_berichte"))

        if not bericht_ids:
            flash("Keine Berichte ausgewählt.", "warning")
            return redirect(url_for("cognos.list_berichte"))

        if aktion == "neues_idv":
            primary_id = request.form.get("primary_bericht_id", "")
            extra_ids  = [str(i) for i in bericht_ids if str(i) != primary_id]
            url = url_for("cognos.als_idv_registrieren",
                          bericht_id=primary_id,
                          extra_bericht_ids=",".join(extra_ids))
            return redirect(url)

        elif aktion == "zu_idv":
            idv_db_id = request.form.get("idv_db_id", "")
            try:
                idv_db_id = int(idv_db_id)
            except (ValueError, TypeError):
                flash("Ungültige IDV-Auswahl.", "error")
                return redirect(url_for("cognos.list_berichte"))

            idv_row = db.execute(
                "SELECT id, idv_id FROM idv_register WHERE id=?", (idv_db_id,)
            ).fetchone()
            if not idv_row:
                flash("IDV nicht gefunden.", "error")
                return redirect(url_for("cognos.list_berichte"))

            linked = 0
            for bid in bericht_ids:
                try:
                    db.execute(
                        "UPDATE cognos_berichte SET idv_register_id=?, bearbeitungsstatus='Registriert'"
                        " WHERE id=? AND bearbeitungsstatus != 'Registriert'",
                        (idv_db_id, bid)
                    )
                    linked += 1
                except Exception:
                    pass
            db.commit()
            flash(f"{linked} Bericht(e) mit IDV {idv_row['idv_id']} verknüpft.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

        flash("Unbekannte Aktion.", "error")
        return redirect(url_for("cognos.list_berichte"))

    # GET
    raw_ids = request.args.getlist("bericht_ids")
    try:
        bericht_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        bericht_ids = []

    if not bericht_ids:
        flash("Keine Berichte ausgewählt.", "warning")
        return redirect(url_for("cognos.list_berichte"))

    ph = ",".join("?" * len(bericht_ids))
    berichte = db.execute(
        f"SELECT * FROM cognos_berichte WHERE id IN ({ph}) ORDER BY berichtsname",
        bericht_ids
    ).fetchall()

    idvs = db.execute("""
        SELECT id, idv_id, bezeichnung FROM idv_register
        WHERE status NOT IN ('Außer Betrieb', 'Abgelöst')
        ORDER BY idv_id
    """).fetchall()

    return render_template("cognos/zusammenfassen.html",
        berichte=berichte,
        idvs=idvs,
    )


@bp.route("/bulk-aktion", methods=["POST"])
@login_required
@write_access_required
def bulk_aktion():
    db      = get_db()
    aktion  = request.form.get("aktion", "")
    raw_ids = request.form.getlist("bericht_ids")

    if aktion == "zusammenfassen":
        ids_qs = "&".join(f"bericht_ids={i}" for i in raw_ids if i)
        return redirect(url_for("cognos.zusammenfassen") + "?" + ids_qs)

    if aktion not in ("ignorieren", "nicht_mehr_ignorieren", "zur_registrierung",
                      "nicht_wesentlich", "owner_aendern", "bewertung_anfordern"):
        flash("Ungültige Aktion.", "error")
        return redirect(url_for("cognos.list_berichte"))

    try:
        bericht_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige IDs.", "error")
        return redirect(url_for("cognos.list_berichte"))

    if not bericht_ids:
        flash("Keine Berichte ausgewählt.", "warning")
        return redirect(url_for("cognos.list_berichte"))

    placeholders = ",".join("?" * len(bericht_ids))

    if aktion == "ignorieren":
        db.execute(
            f"UPDATE cognos_berichte SET bearbeitungsstatus='Ignoriert'"
            f" WHERE id IN ({placeholders}) AND bearbeitungsstatus != 'Registriert'",
            bericht_ids,
        )
        db.commit()
        flash(f"{len(bericht_ids)} Bericht(e) ignoriert.", "info")

    elif aktion == "nicht_mehr_ignorieren":
        db.execute(
            f"UPDATE cognos_berichte SET bearbeitungsstatus='Neu'"
            f" WHERE id IN ({placeholders}) AND bearbeitungsstatus='Ignoriert'",
            bericht_ids,
        )
        db.commit()
        flash(f"{len(bericht_ids)} Bericht(e): Ignorierung aufgehoben.", "success")

    elif aktion == "zur_registrierung":
        db.execute(
            f"UPDATE cognos_berichte SET bearbeitungsstatus='Zur Registrierung'"
            f" WHERE id IN ({placeholders})",
            bericht_ids,
        )
        db.commit()
        flash(f"{len(bericht_ids)} Bericht(e) zur Registrierung vorgemerkt.", "success")

    elif aktion == "nicht_wesentlich":
        db.execute(
            f"UPDATE cognos_berichte SET bearbeitungsstatus='Nicht wesentlich'"
            f" WHERE id IN ({placeholders})",
            bericht_ids,
        )
        db.commit()
        flash(f"{len(bericht_ids)} Bericht(e) als 'Nicht wesentlich' eingestuft.", "success")

    elif aktion == "owner_aendern":
        new_owner = request.form.get("new_owner", "").strip()
        if not new_owner:
            flash("Kein Eigentümer angegeben.", "warning")
        else:
            db.execute(
                f"UPDATE cognos_berichte SET eigentuemer=? WHERE id IN ({placeholders})",
                [new_owner] + bericht_ids,
            )
            db.commit()
            flash(f"{len(bericht_ids)} Bericht(e): Eigentümer auf \"{new_owner}\" gesetzt.", "success")

    elif aktion == "bewertung_anfordern":
        from ..email_service import notify_bericht_bewertung_batch, get_app_base_url
        berichte = db.execute(
            f"SELECT * FROM cognos_berichte WHERE id IN ({placeholders})", bericht_ids
        ).fetchall()

        base_url = get_app_base_url(db)

        grouped: dict[str, list] = {}
        kein_empfaenger = 0
        for bericht in berichte:
            owner = bericht["eigentuemer"] or ""
            email = None
            if owner:
                person = db.execute(
                    "SELECT email FROM persons WHERE (user_id=? OR kuerzel=? OR ad_name=?) AND aktiv=1 AND email IS NOT NULL",
                    (owner, owner, owner)
                ).fetchone()
                if person:
                    email = person["email"]
            if not email:
                kein_empfaenger += 1
                continue
            grouped.setdefault(email, []).append(bericht)

        gesendet = 0
        fehler = 0
        for email, berichte_gruppe in grouped.items():
            try:
                ok = notify_bericht_bewertung_batch(db, berichte_gruppe, email, base_url)
                if ok:
                    gesendet += 1
                else:
                    fehler += 1
            except Exception:
                fehler += 1

        msg_parts = []
        if gesendet:
            n_berichte = sum(len(g) for g in grouped.values())
            msg_parts.append(f"{n_berichte} Bericht(e) in {gesendet} E-Mail(s) gesendet")
        if kein_empfaenger:
            msg_parts.append(f"{kein_empfaenger} ohne zugeordnete E-Mail-Adresse")
        if fehler:
            msg_parts.append(f"{fehler} Fehler beim Versand")
        flash(". ".join(msg_parts) + ".", "success" if gesendet and not fehler else "warning")

    return redirect(url_for("cognos.list_berichte"))
