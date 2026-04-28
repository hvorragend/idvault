from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify, abort, g
from . import (login_required, write_access_required, own_write_required, admin_required,
               get_db, can_write, can_create, can_read_all, current_person_id,
               ROLE_ADMIN)
import sys, os, io, json, re
from datetime import datetime as _dt, timezone as _tz
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import (create_idv, update_idv, change_status, search_idv,
                get_klassifizierungen, get_wesentlichkeitskriterien,
                get_idv_wesentlichkeit, save_idv_wesentlichkeit,
                get_fachliche_testfaelle, get_technischer_test,
                idv_completeness_score)
from db_write_tx import write_tx
from ..db_writer import get_writer
from ..security import (ensure_can_read_idv, ensure_can_write_idv,
                        user_can_read_idv, in_clause)
from ..helpers import _EXT_TO_TYP, _idv_typ_vorschlag, _int_or_none, _safe_referer_url
from ..app_settings import get_bool as _get_bool

bp = Blueprint("eigenentwicklung", __name__, url_prefix="/eigenentwicklung")


# Regulatorische Entwicklungsarten (MaRisk AT 7.2 / DORA).
# Reihenfolge: vom leichtgewichtigsten (Arbeitshilfe) zum regulierten.
ENTWICKLUNGSARTEN = [
    ("arbeitshilfe",
     "Arbeitshilfe",
     "Fachbereich, dezentral, unterhalb der Wesentlichkeitsschwelle."),
    ("idv",
     "IDV",
     "Individuelle Datenverarbeitung – wesentlich, kontrollpflichtig (MaRisk AT 7.2)."),
    ("eigenprogrammierung",
     "Eigenprogrammierung",
     "Interne IT, zentraler IT-Prozess – Code-Qualität, Funktionstrennung."),
    ("auftragsprogrammierung",
     "Auftragsprogrammierung",
     "Externer Dienstleister – DORA-Drittparteien-Risikomanagement."),
]

ENTWICKLUNGSART_LABEL = {key: label for key, label, _desc in ENTWICKLUNGSARTEN}


# U-C3: Smart-Default-Inferenz.
# Mapping IDV-Typ → vorgeschlagene Entwicklungsart + Prüfintervall (Monate).
# Der Benutzer kann die Vorschläge im Formular jederzeit übersteuern – dieses
# Mapping liefert nur die "initiale Vermutung" beim Typ-Wechsel.
_TYP_DEFAULTS = {
    "Excel-Makro":      {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Access-Datenbank": {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Python-Skript":    {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "SQL-Skript":       {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Power-BI-Bericht": {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Cognos-Report":    {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Excel-Tabelle":    {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 24},
    "Sonstige":         {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 24},
    "unklassifiziert":  {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 12},
}


def _normalize_for_prefix(path: str) -> str:
    """Normalisiert Pfade für Präfix-Vergleich: Slashes → Backslash, lower()."""
    if not path:
        return ""
    return path.replace("/", "\\").lower()


def _best_fund_pfad_profil(db, full_paths: list) -> dict | None:
    """Bestimmt das für die Mehrheit der übergebenen Pfade passende Profil.

    Strategie: pro Profil wird gezählt, wie viele der Pfade mit dessen
    ``pfad_praefix`` beginnen. Bei Gleichstand gewinnt das längere Präfix
    (spezifischer). Rückgabe: Profil-Row als dict oder ``None``.
    """
    try:
        profile = db.execute(
            "SELECT * FROM fund_pfad_profile WHERE aktiv=1"
        ).fetchall()
    except Exception:
        return None
    if not profile or not full_paths:
        return None

    norm_paths = [_normalize_for_prefix(p) for p in full_paths if p]
    if not norm_paths:
        return None

    best = None
    best_hits = 0
    best_len  = 0
    for prof in profile:
        praefix = _normalize_for_prefix(prof["pfad_praefix"] or "")
        if not praefix:
            continue
        hits = sum(1 for p in norm_paths if p.startswith(praefix))
        if hits == 0:
            continue
        if (hits > best_hits
                or (hits == best_hits and len(praefix) > best_len)):
            best = dict(prof)
            best_hits = hits
            best_len  = len(praefix)
    return best


def _form_lookups(db):
    """Liefert Nachschlagedaten für IDV-Formulare.

    Quasi-statisch pro Request – Ergebnis wird in ``flask.g`` gecached, damit
    list_idv / new_idv / edit_idv innerhalb eines Requests nicht mehrfach die
    gleichen Lookup-Tabellen abfragen.
    """
    cached = getattr(g, "_form_lookups_cache", None)
    if cached is not None:
        return cached

    result = {
        "org_units":          db.execute("SELECT * FROM org_units WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "persons":            db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall(),
        "geschaeftsprozesse": db.execute("SELECT * FROM geschaeftsprozesse WHERE aktiv=1 ORDER BY gp_nummer").fetchall(),
        "plattformen":        db.execute("SELECT * FROM plattformen WHERE aktiv=1 ORDER BY bezeichnung").fetchall(),
        "idv_typen":               get_klassifizierungen(db, "idv_typ"),
        "pruefintervalle":         get_klassifizierungen(db, "pruefintervall_monate"),
        "nutzungsfrequenzen":      get_klassifizierungen(db, "nutzungsfrequenz"),
        "wesentlichkeitskriterien": get_wesentlichkeitskriterien(db, nur_aktive=True),
        "entwicklungsarten":       ENTWICKLUNGSARTEN,
        "smart_defaults_enabled":  _get_bool(db, "smart_defaults_enabled", True),
    }
    g._form_lookups_cache = result
    return result


_VALID_PER_PAGE_IDV = (25, 50, 100, 200, 500, 1000, 2500)


# ── Liste ──────────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def list_idv():
    db      = get_db()
    q       = request.args.get("q", "")
    status  = request.args.get("status", "")
    filt    = request.args.get("filter", "alle")
    oe_id   = _int_or_none(request.args.get("oe_id"))
    fv_id   = _int_or_none(request.args.get("fv_id"))
    owner_filt = request.args.get("owner", "").strip()
    share_root = request.args.get("share_root", "").strip()
    entwicklungsart = request.args.get("entwicklungsart", "").strip()
    if entwicklungsart not in ENTWICKLUNGSART_LABEL:
        entwicklungsart = ""
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE_IDV:
            session["pref_per_page_idv"] = per_page
    else:
        per_page = session.get("pref_per_page_idv", 100)
    if per_page not in _VALID_PER_PAGE_IDV:
        per_page = 100

    _WESENTLICH = """EXISTS(
        SELECT 1 FROM idv_wesentlichkeit iw
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
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
    else:
        where_parts.append("v.status != 'Archiviert'")
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
    if owner_filt:
        where_parts.append(
            "r.file_id IN (SELECT id FROM idv_files WHERE file_owner = ?)"
        )
        params.append(owner_filt)
    if entwicklungsart:
        where_parts.append("r.entwicklungsart = ?")
        params.append(entwicklungsart)

    # Spezialfilter
    if filt in ("kritisch", "wesentlich"):
        where_parts.append(_WESENTLICH)
    elif filt == "nicht_wesentlich":
        where_parts.append(f"NOT {_WESENTLICH}")
    elif filt == "ueberfaellig":
        where_parts.append("v.pruefstatus = 'ÜBERFÄLLIG'")
    elif filt == "unvollstaendig":
        ids = [r["idv_id"] for r in db.execute("SELECT idv_id FROM v_unvollstaendige_idvs").fetchall()]
        # VULN-L: einheitlicher, sicherer IN-Clause-Helper.
        ph_sql, ph_params = in_clause(ids)
        where_parts.append(f"r.idv_id IN ({ph_sql})")
        params.extend(ph_params)

    # Rollenbasierte Sichtbarkeit
    person_id = current_person_id()
    if not can_read_all():
        if person_id:
            where_parts.append("""(
                r.fachverantwortlicher_id = ?
                OR r.idv_entwickler_id   = ?
                OR r.idv_koordinator_id  = ?
                OR r.stellvertreter_id   = ?
            )""")
            params += [person_id, person_id, person_id, person_id]
        else:
            # Kein Person-Binding und keine Read-All-Rolle → keine Treffer.
            # Verhindert, dass Sonder-Konten ohne persons-Zeile alle IDVs sehen.
            where_parts.append("0")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_sql = f"""
        SELECT COUNT(*) FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        {where_sql}
    """
    total = db.execute(count_sql, params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    sql = f"""
        SELECT r.*, v.*,
          CASE WHEN {_WESENTLICH} THEN 1 ELSE 0 END AS wesentlich_flag,
          EXISTS(SELECT 1 FROM idv_register x WHERE x.vorgaenger_idv_id = r.id) AS hat_nachfolger,
          (CASE WHEN r.file_id IS NOT NULL THEN 1 ELSE 0 END
           + (SELECT COUNT(*) FROM idv_file_links lnk WHERE lnk.idv_db_id = r.id)) AS datei_anzahl,
          f.formula_count        AS file_formula_count,
          f.has_macros           AS file_has_macros,
          f.has_sheet_protection AS file_has_sheet_protection,
          f.file_owner           AS file_owner
        FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        LEFT JOIN idv_files f ON f.id = r.file_id
        {where_sql}
        ORDER BY wesentlich_flag DESC, v.bezeichnung
        LIMIT ? OFFSET ?
    """
    idvs_raw = db.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    # Vollständigkeits-Score (Issue #348) für die aktuelle Seite anreichern.
    # Wird pro Seite einmal berechnet — max. per_page Rows, daher tragbar.
    idvs = []
    for row in idvs_raw:
        d = dict(row)
        d["completeness_score"] = idv_completeness_score(db, row["id"])["score"]
        idvs.append(d)

    # Filter-Optionen für Dropdowns
    org_units = db.execute(
        "SELECT id, bezeichnung FROM org_units WHERE aktiv=1 ORDER BY bezeichnung"
    ).fetchall()
    persons_fv = db.execute(
        "SELECT DISTINCT p.id, p.nachname, p.vorname FROM persons p"
        " WHERE p.aktiv=1"
        " AND EXISTS (SELECT 1 FROM idv_register r WHERE r.fachverantwortlicher_id = p.id)"
        " ORDER BY p.nachname"
    ).fetchall()
    share_roots = [
        r["share_root"] for r in db.execute(
            "SELECT DISTINCT share_root FROM idv_files WHERE share_root IS NOT NULL AND status='active' ORDER BY share_root"
        ).fetchall()
    ]
    owner_list = [
        r["file_owner"] for r in db.execute(
            "SELECT DISTINCT file_owner FROM idv_files"
            " WHERE file_owner IS NOT NULL AND file_owner != '' AND status='active'"
            " ORDER BY file_owner"
        ).fetchall()
    ]

    from . import ROLE_ADMIN
    is_admin = (session.get("user_role") == ROLE_ADMIN)
    return render_template("eigenentwicklung/list.html", idvs=idvs, can_write=can_write(),
                           can_create=can_create(),
                           is_admin=is_admin,
                           org_units=org_units, persons_fv=persons_fv,
                           share_roots=share_roots,
                           owner_list=owner_list, owner_filt=owner_filt,
                           total=total, total_pages=total_pages,
                           page=page, per_page=per_page,
                           valid_per_page=_VALID_PER_PAGE_IDV,
                           q=q, status=status, filt=filt,
                           oe_id=oe_id, fv_id=fv_id,
                           share_root=share_root,
                           entwicklungsart=entwicklungsart,
                           entwicklungsarten=ENTWICKLUNGSARTEN,
                           entwicklungsart_label=ENTWICKLUNGSART_LABEL)


# ── Wesentliche Eigenentwicklungen ────────────────────────────────────────

@bp.route("/wesentlich")
@login_required
def wesentliche_idvs():
    """Dedizierte Seite: Wesentliche Eigenentwicklungen."""
    # Wenn der Filter-Dropdown auf etwas anderes umgestellt wird, zur Hauptliste weiterleiten
    filt_override = request.args.get("filter")
    if filt_override and filt_override != "wesentlich":
        return redirect(url_for("eigenentwicklung.list_idv", **request.args))

    db      = get_db()
    q       = request.args.get("q", "")
    status  = request.args.get("status", "")
    oe_id   = _int_or_none(request.args.get("oe_id"))
    fv_id   = _int_or_none(request.args.get("fv_id"))
    owner_filt = request.args.get("owner", "").strip()
    share_root = request.args.get("share_root", "").strip()
    entwicklungsart = request.args.get("entwicklungsart", "").strip()
    if entwicklungsart not in ENTWICKLUNGSART_LABEL:
        entwicklungsart = ""
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE_IDV:
            session["pref_per_page_idv"] = per_page
    else:
        per_page = session.get("pref_per_page_idv", 100)
    if per_page not in _VALID_PER_PAGE_IDV:
        per_page = 100

    _WESENTLICH = """EXISTS(
        SELECT 1 FROM idv_wesentlichkeit iw
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
    )"""

    where_parts = [_WESENTLICH, "v.status != 'Archiviert'"]
    params: list = []

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
    if owner_filt:
        where_parts.append(
            "r.file_id IN (SELECT id FROM idv_files WHERE file_owner = ?)"
        )
        params.append(owner_filt)
    if entwicklungsart:
        where_parts.append("r.entwicklungsart = ?")
        params.append(entwicklungsart)

    person_id = current_person_id()
    if not can_read_all():
        if person_id:
            where_parts.append("""(
                r.fachverantwortlicher_id = ?
                OR r.idv_entwickler_id   = ?
                OR r.idv_koordinator_id  = ?
                OR r.stellvertreter_id   = ?
            )""")
            params += [person_id, person_id, person_id, person_id]
        else:
            where_parts.append("0")

    where_sql = "WHERE " + " AND ".join(where_parts)

    total = db.execute(
        f"""SELECT COUNT(*) FROM v_idv_uebersicht v
            JOIN idv_register r ON r.idv_id = v.idv_id
            {where_sql}""",
        params,
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    idvs_raw = db.execute(f"""
        SELECT r.*, v.*,
          CASE WHEN {_WESENTLICH} THEN 1 ELSE 0 END AS wesentlich_flag,
          EXISTS(SELECT 1 FROM idv_register x WHERE x.vorgaenger_idv_id = r.id) AS hat_nachfolger,
          (CASE WHEN r.file_id IS NOT NULL THEN 1 ELSE 0 END
           + (SELECT COUNT(*) FROM idv_file_links lnk WHERE lnk.idv_db_id = r.id)) AS datei_anzahl,
          f.formula_count        AS file_formula_count,
          f.has_macros           AS file_has_macros,
          f.has_sheet_protection AS file_has_sheet_protection,
          f.file_owner           AS file_owner
        FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        LEFT JOIN idv_files f ON f.id = r.file_id
        {where_sql}
        ORDER BY v.bezeichnung
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()

    # Vollständigkeits-Score pro Zeile anreichern (analog list_idv)
    idvs = []
    for row in idvs_raw:
        d = dict(row)
        d["completeness_score"] = idv_completeness_score(db, row["id"])["score"]
        idvs.append(d)

    org_units = db.execute(
        "SELECT id, bezeichnung FROM org_units WHERE aktiv=1 ORDER BY bezeichnung"
    ).fetchall()
    persons_fv = db.execute(
        "SELECT DISTINCT p.id, p.nachname, p.vorname FROM persons p"
        " WHERE p.aktiv=1"
        " AND EXISTS (SELECT 1 FROM idv_register r WHERE r.fachverantwortlicher_id = p.id)"
        " ORDER BY p.nachname"
    ).fetchall()
    share_roots = [
        r["share_root"] for r in db.execute(
            "SELECT DISTINCT share_root FROM idv_files WHERE share_root IS NOT NULL AND status='active' ORDER BY share_root"
        ).fetchall()
    ]
    owner_list = [
        r["file_owner"] for r in db.execute(
            "SELECT DISTINCT file_owner FROM idv_files"
            " WHERE file_owner IS NOT NULL AND file_owner != '' AND status='active'"
            " ORDER BY file_owner"
        ).fetchall()
    ]

    from . import ROLE_ADMIN
    is_admin = (session.get("user_role") == ROLE_ADMIN)
    return render_template("eigenentwicklung/list.html", idvs=idvs, can_write=can_write(),
                           can_create=can_create(),
                           is_admin=is_admin,
                           org_units=org_units, persons_fv=persons_fv,
                           share_roots=share_roots,
                           owner_list=owner_list, owner_filt=owner_filt,
                           total=total, total_pages=total_pages,
                           page=page, per_page=per_page,
                           valid_per_page=_VALID_PER_PAGE_IDV,
                           q=q, status=status, filt="wesentlich",
                           oe_id=oe_id, fv_id=fv_id,
                           share_root=share_root,
                           entwicklungsart=entwicklungsart,
                           entwicklungsarten=ENTWICKLUNGSARTEN,
                           entwicklungsart_label=ENTWICKLUNGSART_LABEL)


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
               r.idv_typ, ou.bezeichnung AS oe_bezeichnung
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
            "oe":       row["oe_bezeichnung"] or "",
            "url":      url_for("eigenentwicklung.detail_idv", idv_db_id=row["id"]),
        }
        for row in rows
    ])


# ── Smart-Default-Inferenz (U-C3) ─────────────────────────────────────────

@bp.route("/api/infer")
@login_required
def api_infer():
    """Liefert Feld-Vorschläge zu einem IDV-Typ.

    Wird vom Formular-JS beim onchange des Typ-Dropdowns aufgerufen, um
    Entwicklungsart und Prüfintervall automatisch vorzuschlagen. Der Client
    übernimmt die Werte nur, wenn der Benutzer das Feld noch nicht explizit
    geändert hat.
    """
    if not _get_bool(get_db(), "smart_defaults_enabled", True):
        return jsonify({}), 204
    typ = (request.args.get("idv_typ") or "").strip()
    defaults = _TYP_DEFAULTS.get(typ, _TYP_DEFAULTS["unklassifiziert"])
    return jsonify({
        "idv_typ":               typ,
        "entwicklungsart":       defaults["entwicklungsart"],
        "pruefintervall_monate": defaults["pruefintervall_monate"],
    })


# ── Bulk-Löschen (Admin) ───────────────────────────────────────────────────

@bp.route("/bulk-loeschen", methods=["POST"])
@admin_required
def bulk_loeschen():
    """Löscht mehrere Eigenentwicklungen auf einmal (nur IDV-Administrator)."""
    db        = get_db()
    person_id = session.get("person_id")
    raw_ids   = request.form.getlist("idv_ids")

    _raw_next = request.form.get("next", "").strip()
    _back = _raw_next if _raw_next.startswith("/") else url_for("eigenentwicklung.list_idv")

    try:
        idv_db_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige IDs.", "error")
        return redirect(_back)

    if not idv_db_ids:
        flash("Keine Eigenentwicklungen ausgewählt.", "warning")
        return redirect(_back)

    existing_ids = [
        r["id"] for r in db.execute(
            f"SELECT id FROM idv_register WHERE id IN ({','.join(['?']*len(idv_db_ids))})",
            idv_db_ids,
        ).fetchall()
    ]

    def _do(c):
        with write_tx(c):
            for idv_db_id in existing_ids:
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Neu' "
                    "WHERE bearbeitungsstatus='Registriert' AND id IN ("
                    "  SELECT file_id FROM idv_register WHERE id=? AND file_id IS NOT NULL"
                    "  UNION"
                    "  SELECT file_id FROM idv_file_links WHERE idv_db_id=?"
                    ")",
                    (idv_db_id, idv_db_id),
                )
                c.execute("DELETE FROM idv_history        WHERE idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM massnahmen          WHERE idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM pruefungen          WHERE idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM genehmigungen       WHERE idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM dokumente           WHERE idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM idv_abhaengigkeiten WHERE quell_idv_id = ?", (idv_db_id,))
                c.execute("DELETE FROM idv_abhaengigkeiten WHERE ziel_idv_id  = ?", (idv_db_id,))
                c.execute("UPDATE idv_register SET vorgaenger_idv_id = NULL WHERE vorgaenger_idv_id = ?",
                          (idv_db_id,))
                c.execute("DELETE FROM idv_register WHERE id=?", (idv_db_id,))
        return len(existing_ids)

    deleted = get_writer().submit(_do, wait=True)
    flash(f"{deleted} Eigenentwicklung(en) gelöscht.", "success")
    return redirect(_back)


# ── Bulk-Statusänderung (Admin + Koordinator) ─────────────────────────────

_BULK_STATUS_ERLAUBT = [
    "Entwurf", "In Prüfung", "Freigegeben", "Freigegeben mit Auflagen",
    "Abgelehnt", "Abgekündigt", "Archiviert",
]

@bp.route("/bulk-status", methods=["POST"])
@write_access_required
def bulk_status():
    """Setzt den Status mehrerer Eigenentwicklungen auf einmal (Admin + Koordinator)."""
    db        = get_db()
    person_id = session.get("person_id")
    raw_ids   = request.form.getlist("idv_ids")
    neuer_status = request.form.get("neuer_status", "").strip()

    _raw_next = request.form.get("next", "").strip()
    _back = _raw_next if _raw_next.startswith("/") else url_for("eigenentwicklung.list_idv")

    if neuer_status not in _BULK_STATUS_ERLAUBT:
        flash("Bitte einen gültigen Zielstatus auswählen.", "error")
        return redirect(_back)

    try:
        idv_db_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige IDs.", "error")
        return redirect(_back)

    if not idv_db_ids:
        flash("Keine Eigenentwicklungen ausgewählt.", "warning")
        return redirect(_back)

    user_name = session.get("user_name", "")
    writer = get_writer()
    updated = errors = 0
    for idv_db_id in idv_db_ids:
        try:
            writer.submit(
                lambda c, _id=idv_db_id, _st=neuer_status, _p=person_id, _n=user_name:
                    change_status(c, _id, _st, geaendert_von_id=_p, bearbeiter_name=_n),
                wait=True,
            )
            updated += 1
        except Exception as exc:
            # VULN-011: Einzel-Fehler nicht schlucken – damit Batch-Fehler
            # im Log nachvollziehbar bleiben (z.B. ungültige Übergänge,
            # DB-Contention).
            errors += 1
            from flask import current_app
            current_app.logger.warning(
                "Bulk-Status-Änderung für Eigenentwicklung %s auf '%s' fehlgeschlagen: %s",
                idv_db_id, neuer_status, exc,
            )

    msg = f'{updated} Eigenentwicklung(en) auf "{neuer_status}" gesetzt.'
    if errors:
        msg += f" {errors} konnten nicht geändert werden."

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "msg": msg, "updated": updated, "errors": errors,
                        "neuer_status": neuer_status})

    flash(msg, "success" if not errors else "warning")
    return redirect(_back)


# ── Detail ─────────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>")
@login_required
def detail_idv(idv_db_id):
    db  = get_db()
    # VULN-E: Fachverantwortliche dürfen nur eigene IDVs einsehen.
    ensure_can_read_idv(db, idv_db_id)
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
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

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

    # Verknüpfte Cognos-Berichte (Issue #456): Berichtsabzug-Daten
    # (Suchpfad, Eigentümer, Kennzahlen, Komplexität …) im Detail anzeigen.
    try:
        cognos_berichte = db.execute("""
            SELECT cb.*,
                   p_imp.nachname || ', ' || p_imp.vorname AS importiert_von_name
            FROM cognos_berichte cb
            LEFT JOIN persons p_imp ON cb.importiert_von_id = p_imp.id
            WHERE cb.idv_register_id = ?
            ORDER BY cb.berichtsname
        """, (idv_db_id,)).fetchall()
    except Exception:
        cognos_berichte = []

    _raw_history = db.execute("""
        SELECT h.*,
               COALESCE(p.nachname || ', ' || p.vorname, h.bearbeiter_name) AS person
        FROM idv_history h
        LEFT JOIN persons p ON h.durchgefuehrt_von_id = p.id
        WHERE h.idv_id = ?
        ORDER BY h.durchgefuehrt_am DESC
        LIMIT 50
    """, (idv_db_id,)).fetchall()

    _FELD_LABELS = {
        "bezeichnung": "Bezeichnung", "version": "Version",
        "idv_typ": "Typ", "entwicklungsart": "Art", "status": "Status",
        "fachverantwortlicher_id": "Fachverantwortlicher",
        "idv_entwickler_id": "Entwickler", "idv_koordinator_id": "Koordinator",
        "stellvertreter_id": "Stellvertreter", "org_unit_id": "Org.-Einheit",
        "gp_id": "Geschäftsprozess", "naechste_pruefung": "Nächste Prüfung",
        "pruefintervall_monate": "Prüfintervall", "teststatus": "Teststatus",
        "anwenderdokumentation": "Anwenderdokumentation", "datenschutz_beachtet": "Datenschutz eingehalten",
        "zellschutz_formeln": "Zellschutz Formeln", "plattform_id": "Plattform",
        "nutzungsfrequenz": "Nutzungsfrequenz",
    }
    history = []
    for h in _raw_history:
        row = dict(h)
        if h["geaenderte_felder"]:
            try:
                chg = json.loads(h["geaenderte_felder"])
                row["aenderungen_summary"] = ", ".join(
                    _FELD_LABELS.get(k, k) for k in chg
                )
            except Exception:
                row["aenderungen_summary"] = ""
        else:
            row["aenderungen_summary"] = ""
        history.append(row)

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
                   p_z.nachname || ', ' || p_z.vorname AS zugewiesen_an,
                   p_c.nachname || ', ' || p_c.vorname AS bearbeitet_von,
                   pool.name AS pool_name
            FROM idv_freigaben f
            LEFT JOIN persons p_b ON f.beauftragt_von_id    = p_b.id
            LEFT JOIN persons p_d ON f.durchgefuehrt_von_id = p_d.id
            LEFT JOIN persons p_z ON f.zugewiesen_an_id     = p_z.id
            LEFT JOIN persons p_c ON f.bearbeitet_von_id    = p_c.id
            LEFT JOIN freigabe_pools pool ON f.pool_id      = pool.id
            WHERE f.idv_id = ?
            ORDER BY f.erstellt_am
        """, (idv_db_id,)).fetchall()
    except Exception:
        freigaben = []

    # Personen für Freigabeanforderer-Auswahl
    freigabe_persons = db.execute(
        "SELECT id, nachname, vorname, rolle FROM persons WHERE aktiv=1 ORDER BY nachname"
    ).fetchall()

    # Aktive Freigabe-Pools für Pool-Zuweisung beim Schritt-Anlegen (U-D4)
    try:
        freigabe_pools = db.execute(
            "SELECT id, name FROM freigabe_pools WHERE aktiv=1 ORDER BY name"
        ).fetchall()
    except Exception:
        freigabe_pools = []

    ist_wesentlich = any(k["erfuellt"] for k in wesentlichkeit)

    # Phasenstatus für die Freigabe-Anzeige. Für Patch-Freigaben (#320)
    # kann der effektive Schritte-Katalog pro Phase kleiner sein; die
    # ``_erledigt``-Flags beziehen sich dann auf die tatsächlich für diese
    # IDV aktiven Schritte (sonst erscheint z.B. ein Patch-Verfahren
    # dauerhaft als „Phase 1 noch nicht erledigt", weil ein nicht
    # angelegter Schritt fehlt).
    from .freigaben import (_active_phase_schritte as _aps_schritte,
                             _get_kategorie as _freigabe_kategorie,
                             _ist_gda4_oder_dora_kritisch as _ist_gda4,
                             _get_patch_schritte as _freigabe_patch_schritte)
    _PHASE_1 = ["Fachlicher Test", "Technischer Test"]
    _PHASE_2 = ["Fachliche Abnahme", "Technische Abnahme"]
    _PHASE_3 = ["Archivierung Originaldatei"]
    aktive_p1, aktive_p2, aktive_p3 = _aps_schritte(db, idv_db_id)
    phase1_schritte   = [f for f in freigaben if f["schritt"] in _PHASE_1]
    phase2_schritte   = [f for f in freigaben if f["schritt"] in _PHASE_2]
    phase3_schritte   = [f for f in freigaben if f["schritt"] in _PHASE_3]
    # Für Patch-Konfigurationen ohne Phase-1-Schritt liefern angelegte
    # Schritte allein kein verlässliches Signal, ob das Verfahren schon
    # gestartet wurde — die Einstufung (Kategorie-Spalte) wird mitgezählt.
    phase1_gestartet  = (len(phase1_schritte) > 0
                         or bool(idv["freigabe_aenderungskategorie"]))
    phase1_erledigt   = (not aktive_p1) or (
        set(aktive_p1).issubset(
            {f["schritt"] for f in phase1_schritte if f["status"] == "Erledigt"}
        )
    )
    phase2_gestartet  = len(phase2_schritte) > 0
    phase2_erledigt   = (not aktive_p2) or (
        set(aktive_p2).issubset(
            {f["schritt"] for f in phase2_schritte if f["status"] == "Erledigt"}
        )
    )
    phase3_gestartet  = len(phase3_schritte) > 0
    phase3_erledigt   = (not aktive_p3) or (
        set(aktive_p3).issubset(
            {f["schritt"] for f in phase3_schritte if f["status"] == "Erledigt"}
        )
    )
    hat_offenen_schritt = any(
        f["status"] == "Ausstehend" and f["schritt"] not in _PHASE_3
        for f in freigaben
    )
    freigabe_kategorie     = _freigabe_kategorie(db, idv_db_id)
    freigabe_gda4_gesperrt = _ist_gda4(db, idv_db_id)
    freigabe_patch_schritte = _freigabe_patch_schritte(db)
    hat_vorgaenger         = bool(idv["vorgaenger_idv_id"])

    fachliche_testfaelle = get_fachliche_testfaelle(db, idv_db_id)
    technischer_test     = get_technischer_test(db, idv_db_id)

    # Flags, ob die eigentlichen Test-Einträge vorhanden sind. Wird für die
    # Anzeige der "Anlage XYZ-Test"-Buttons verwendet, falls ein Eintrag
    # manuell gelöscht wurde.
    fachlich_vorhanden  = bool(fachliche_testfaelle)
    technisch_vorhanden = technischer_test is not None

    completeness = idv_completeness_score(db, idv_db_id)

    pruefungen = db.execute("""
        SELECT p.*, per.nachname || ', ' || per.vorname AS pruefer
        FROM pruefungen p
        LEFT JOIN persons per ON p.pruefer_id = per.id
        WHERE p.idv_id = ?
        ORDER BY p.pruefungsdatum DESC
    """, (idv_db_id,)).fetchall()

    # Excel-Dateien ohne Zell-/Blattschutz für das Inline-Modal der
    # Fachlichen Abnahme — muss dort bestätigt werden können.
    from .freigaben import _unprotected_excel_files_for_idv
    ungeschuetzte_excel = _unprotected_excel_files_for_idv(db, idv_db_id)

    # Issue #351: Stille Freigabe verfuegbar?
    silent_release_enabled = False
    try:
        s_row = db.execute(
            "SELECT value FROM app_settings WHERE key='silent_release_enabled'"
        ).fetchone()
        silent_release_enabled = bool(s_row) and (s_row["value"] or "").strip() in ("1", "true", "yes")
    except Exception:
        pass
    silent_release_moeglich = (
        silent_release_enabled
        and not ist_wesentlich
        and not phase1_gestartet
        and idv["status"] not in (
            "Freigegeben", "Freigegeben mit Auflagen",
            "Freigegeben (Stille Freigabe)",
        )
    )

    return render_template("eigenentwicklung/detail.html",
        idv=idv, file=file, extra_files=extra_files,
        cognos_berichte=cognos_berichte,
        history=history, massnahmen=massnahmen,
        wesentlichkeit=wesentlichkeit,
        vorgaenger=vorgaenger, nachfolger=nachfolger,
        freigaben=freigaben, ist_wesentlich=ist_wesentlich,
        freigabe_persons=freigabe_persons,
        freigabe_pools=freigabe_pools,
        phase1_gestartet=phase1_gestartet, phase1_erledigt=phase1_erledigt,
        phase2_gestartet=phase2_gestartet, phase2_erledigt=phase2_erledigt,
        phase3_gestartet=phase3_gestartet, phase3_erledigt=phase3_erledigt,
        hat_offenen_schritt=hat_offenen_schritt,
        teststatus_werte=_TESTSTATUS_WERTE,
        fachliche_testfaelle=fachliche_testfaelle,
        technischer_test=technischer_test,
        fachlich_vorhanden=fachlich_vorhanden,
        technisch_vorhanden=technisch_vorhanden,
        entwicklungsart_label=ENTWICKLUNGSART_LABEL,
        freigabe_kategorie=freigabe_kategorie,
        freigabe_gda4_gesperrt=freigabe_gda4_gesperrt,
        freigabe_patch_schritte=freigabe_patch_schritte,
        hat_vorgaenger=hat_vorgaenger,
        completeness=completeness,
        pruefungen=pruefungen,
        ungeschuetzte_excel=ungeschuetzte_excel,
        silent_release_moeglich=silent_release_moeglich,
        can_create=can_create())


# ── Draft-Persistenz ───────────────────────────────────────────────────────


def _draft_user_id() -> str | None:
    return session.get("user_id")


def _upsert_draft(c, user_id: str, draft_json: str, now: str) -> None:
    with write_tx(c):
        c.execute("""
            INSERT INTO idv_draft (user_id, draft_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
               SET draft_json = excluded.draft_json,
                   updated_at = excluded.updated_at
        """, (user_id, draft_json, now, now))


def _discard_draft(c, user_id: str) -> None:
    with write_tx(c):
        c.execute("DELETE FROM idv_draft WHERE user_id = ?", (user_id,))


@bp.route("/draft", methods=["POST"])
@own_write_required
def draft_save():
    uid = _draft_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    now = _dt.now(_tz.utc).isoformat()
    payload = {k: v for k, v in request.form.items()
               if k not in ("csrf_token", "save_action")}
    draft_json = json.dumps(payload, ensure_ascii=False)
    get_writer().submit(lambda c: _upsert_draft(c, uid, draft_json, now), wait=True)
    return jsonify({"ok": True, "updated_at": now})


@bp.route("/draft", methods=["DELETE"])
@own_write_required
def draft_delete():
    uid = _draft_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    get_writer().submit(lambda c: _discard_draft(c, uid), wait=True)
    return jsonify({"ok": True})


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
        person_id  = session.get("person_id")
        user_name  = session.get("user_name", "")
        # Ohne Entwickler-Eintrag würde der Ersteller nach dem Speichern den
        # Schreibzugriff verlieren (Ownership-Check in security.py). Daher als
        # Fallback den eingeloggten User als Entwickler eintragen.
        if not data.get("idv_entwickler_id") and person_id:
            data["idv_entwickler_id"] = person_id
        try:
            antworten = _build_wesentlichkeit_answers(db, request.form)
            extra_raw = request.form.get("extra_file_ids", "")
            extras = []
            for part in extra_raw.split(","):
                extra_id = _int_or_none(part.strip())
                if extra_id and extra_id != file_id:
                    extras.append(extra_id)

            draft_uid = _draft_user_id()

            def _do(c):
                with write_tx(c):
                    new_id = create_idv(c, data, erfasser_id=person_id,
                                        bearbeiter_name=user_name, commit=False)
                    if antworten:
                        save_idv_wesentlichkeit(c, new_id, antworten, commit=False)
                    for extra_id in extras:
                        c.execute(
                            "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                            (new_id, extra_id),
                        )
                        c.execute(
                            "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id=?",
                            (extra_id,),
                        )
                    if draft_uid:
                        c.execute("DELETE FROM idv_draft WHERE user_id = ?", (draft_uid,))
                return new_id

            new_id = get_writer().submit(_do, wait=True)
            flash("Eigenentwicklung erfolgreich angelegt.", "success")
            if request.form.get("save_action") == "save_and_new":
                return redirect(url_for("eigenentwicklung.new_idv"))
            if request.form.get("save_action") == "save_and_close":
                return redirect(url_for("eigenentwicklung.list_idv"))
            return redirect(url_for("eigenentwicklung.edit_idv", idv_db_id=new_id))
        except Exception as e:
            flash(f"Fehler beim Speichern: {e}", "error")

    # Optionales Vorausfüllen aus Scannerfund
    fund          = None
    prefill       = {}
    extra_fonds   = []
    hash_duplikate = []
    draft_info    = None
    file_id       = _int_or_none(request.args.get("file_id"))
    extra_file_ids = request.args.get("extra_file_ids", "")

    # U-D1: Gespeicherten Entwurf laden (nur wenn kein Scanner-Fund-Kontext).
    if not file_id:
        uid = _draft_user_id()
        if uid:
            drow = db.execute(
                "SELECT draft_json, updated_at FROM idv_draft WHERE user_id = ?", (uid,)
            ).fetchone()
            if drow:
                try:
                    draft_data = json.loads(drow["draft_json"])
                    draft_info = {"updated_at": drow["updated_at"], "data": draft_data}
                    prefill.update(draft_data)
                except Exception:
                    pass

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
            # Rechenformeln durch Zellschutz gesichert → aus Scan vorbelegen,
            # wenn die Datei Formeln enthält UND mind. ein Blattschutz aktiv ist.
            if (fund["formula_count"] or 0) > 0 and fund["has_sheet_protection"]:
                prefill["zellschutz_formeln"] = 1
            # Datei-Eigentümer als Entwickler vorbelegen
            owner_hint = fund["file_owner"] or fund["office_author"] or ""
            if owner_hint:
                # DOMAIN\Username → Username (AD-Login ohne Domain-Präfix)
                ad_login = owner_hint.split("\\")[-1] if "\\" in owner_hint else owner_hint
                dev_row = db.execute(
                    """SELECT id FROM persons WHERE aktiv=1 AND (
                        user_id=? OR ad_name=?
                        OR (vorname || ' ' || nachname)=?
                        OR (nachname || ', ' || vorname)=?
                        OR (nachname || ' ' || vorname)=?
                    ) LIMIT 1""",
                    (ad_login, ad_login, owner_hint, owner_hint, owner_hint)
                ).fetchone()
                if dev_row:
                    prefill["idv_entwickler_id"] = dev_row["id"]
        # Zusätzliche Dateien für Banner laden
        if extra_file_ids:
            extra_ids_parsed = [_int_or_none(x.strip()) for x in extra_file_ids.split(",") if x.strip()]
            extra_ids_parsed = [i for i in extra_ids_parsed if i and i != file_id]
            if extra_ids_parsed:
                ph, ph_params = in_clause(extra_ids_parsed)
                extra_fonds = db.execute(
                    f"SELECT * FROM idv_files WHERE id IN ({ph})", ph_params
                ).fetchall()

        # Hash-basierte Auto-Gruppierung: prüfen, ob ein Scannerfund mit identischem
        # Hash bereits einer IDV zugeordnet ist, und dem Nutzer ein Verknüpfen anbieten.
        hash_candidates = []
        if fund and fund["file_hash"] and fund["file_hash"] != "HASH_ERROR":
            hash_candidates.append(fund["file_hash"])
        for ef in extra_fonds:
            h = ef["file_hash"]
            if h and h != "HASH_ERROR" and h not in hash_candidates:
                hash_candidates.append(h)
        if hash_candidates:
            own_ids = [file_id] + [ef["id"] for ef in extra_fonds]
            ph_hash, ph_hash_params = in_clause(hash_candidates)
            ph_own,  ph_own_params  = in_clause(own_ids)
            hash_duplikate = db.execute(f"""
                SELECT DISTINCT r.id AS idv_db_id, r.idv_id, r.bezeichnung, r.status,
                       f.id AS match_file_id, f.file_name AS match_file_name,
                       f.file_hash AS match_hash
                FROM idv_files f
                LEFT JOIN idv_register  reg ON reg.file_id = f.id
                LEFT JOIN idv_file_links lnk ON lnk.file_id = f.id
                LEFT JOIN idv_register  r    ON r.id = COALESCE(reg.id, lnk.idv_db_id)
                WHERE f.file_hash IN ({ph_hash})
                  AND f.id NOT IN ({ph_own})
                  AND r.id IS NOT NULL
                ORDER BY r.idv_id
            """, ph_hash_params + ph_own_params).fetchall()

        # Nicht-registrierte Duplikate (gleicher Hash, keine IDV-Zuordnung) automatisch
        # zur Gruppe hinzufügen, sofern der Nutzer keine explizite Auswahl getroffen hat.
        if fund and fund["file_hash"] and fund["file_hash"] != "HASH_ERROR" and not extra_file_ids:
            already_ids = [file_id] + [ef["id"] for ef in extra_fonds]
            ph_sib, ph_sib_params = in_clause(already_ids)
            siblings = db.execute(f"""
                SELECT * FROM idv_files
                WHERE file_hash = ?
                  AND status = 'active'
                  AND id NOT IN ({ph_sib})
                  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = idv_files.id)
                  AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = idv_files.id)
            """, [fund["file_hash"]] + ph_sib_params).fetchall()
            if siblings:
                extra_fonds = list(extra_fonds) + list(siblings)
                prefill["extra_file_ids"] = ",".join(str(s["id"]) for s in siblings)

    # Entwickler-Default: wenn keiner aus dem Scanner-Owner ableitbar ist,
    # den eingeloggten User vorbelegen — er behält damit Schreibrechte
    # (Ownership-Check in security.py).
    if not prefill.get("idv_entwickler_id"):
        my_pid = current_person_id()
        if my_pid:
            prefill["idv_entwickler_id"] = my_pid

    # OE-Default vorschlagen: Entwickler-OE (aus Stammdaten) bevorzugen,
    # ansonsten OE des eingeloggten Benutzers (U-C3-Fallback).
    if not prefill.get("org_unit_id"):
        dev_pid = _int_or_none(prefill.get("idv_entwickler_id"))
        if dev_pid:
            dev_row = db.execute(
                "SELECT org_unit_id FROM persons WHERE id = ?", (dev_pid,)
            ).fetchone()
            if dev_row and dev_row["org_unit_id"]:
                prefill["org_unit_id"] = dev_row["org_unit_id"]
    if not prefill.get("org_unit_id"):
        my_pid = current_person_id()
        if my_pid:
            me = db.execute(
                "SELECT org_unit_id FROM persons WHERE id = ?", (my_pid,)
            ).fetchone()
            if me and me["org_unit_id"]:
                prefill["org_unit_id"] = me["org_unit_id"]

    cancel_url = _safe_referer_url(request, url_for("eigenentwicklung.list_idv"))

    return render_template("eigenentwicklung/form.html", idv=None,
                           fund=fund, prefill=prefill,
                           extra_fonds=extra_fonds,
                           hash_duplikate=hash_duplikate,
                           draft_info=draft_info,
                           wesentlichkeit_antworten={},
                           can_write=can_write(),
                           cancel_url=cancel_url,
                           **_form_lookups(db))


# ── Schnell-Anlage (Issue #348) ────────────────────────────────────────────

@bp.route("/neu-quick", methods=["GET", "POST"])
@own_write_required
def new_idv_quick():
    """Schnell-Anlage mit drei Pflichtfeldern (Bezeichnung, Verantwortlicher,
    vorläufige Klassifikation). Weitere Felder werden in der Nachpflege
    ergänzt; ein ``idv_incomplete_reminder`` erinnert den Verantwortlichen,
    und ``/freigaben/.../starten`` bleibt gesperrt, bis der Vollständigkeits-
    Score 100 % erreicht."""
    db = get_db()
    if request.method == "POST":
        bezeichnung = (request.form.get("bezeichnung") or "").strip()
        fv_id       = _int_or_none(request.form.get("fachverantwortlicher_id"))
        idv_typ     = (request.form.get("idv_typ") or "unklassifiziert").strip()

        if not bezeichnung or not fv_id:
            flash("Bitte Bezeichnung und Verantwortlichen angeben.", "error")
            return redirect(url_for("eigenentwicklung.new_idv_quick"))

        person_id = session.get("person_id")
        user_name = session.get("user_name", "")
        data = {
            "bezeichnung":             bezeichnung,
            "fachverantwortlicher_id": fv_id,
            "idv_typ":                 idv_typ,
            # Ersteller ist Entwickler, sonst greift der Ownership-Check beim
            # späteren Edit nicht.
            "idv_entwickler_id":       person_id,
            # Sinnvolle Defaults für die Zwei-Phasen-Anlage; Entwicklungsart
            # und Prüfintervall werden aus dem Typ-Default übernommen, falls
            # vorhanden, sonst greifen die Fallbacks in create_idv().
            "entwicklungsart":         _TYP_DEFAULTS.get(
                idv_typ, {}
            ).get("entwicklungsart", "arbeitshilfe"),
            "pruefintervall_monate":   _TYP_DEFAULTS.get(
                idv_typ, {}
            ).get("pruefintervall_monate", 12),
        }

        def _do(c):
            with write_tx(c):
                return create_idv(c, data, erfasser_id=person_id,
                                  bearbeiter_name=user_name, commit=False)
        try:
            new_id = get_writer().submit(_do, wait=True)
        except Exception as e:
            flash(f"Fehler beim Anlegen: {e}", "error")
            return redirect(url_for("eigenentwicklung.new_idv_quick"))

        flash("Eigenentwicklung schnell angelegt. Bitte in den nächsten "
              "14 Tagen die verbleibenden Felder nachpflegen.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=new_id))

    # GET: Default-Verantwortlicher = aktuell eingeloggte Person (falls vorhanden)
    prefill = {}
    my_pid = current_person_id()
    if my_pid:
        prefill["fachverantwortlicher_id"] = my_pid

    lookups = _form_lookups(db)
    cancel_url = _safe_referer_url(request, url_for("eigenentwicklung.list_idv"))
    return render_template(
        "eigenentwicklung/form_quick.html",
        prefill=prefill,
        persons=lookups["persons"],
        idv_typen=lookups["idv_typen"],
        cancel_url=cancel_url,
    )


# ── Bulk-Registrierung (P3) ────────────────────────────────────────────────

@bp.route("/bulk-neu", methods=["GET", "POST"])
@own_write_required
def bulk_neu():
    """Legt aus einer Menge Scanner-Funden je einen eigenen IDV-Eintrag an.

    Pro Datei eine IDV. Gemeinsame Felder (OE, Fachverantwortlicher,
    Entwicklungsart) werden aus dem Kopfbereich übernommen, pro Zeile
    können Bezeichnung und IDV-Typ individuell gesetzt werden.
    """
    db = get_db()

    if request.method == "POST":
        raw_ids = request.form.getlist("file_ids")
        try:
            file_ids = [int(i) for i in raw_ids if i]
        except ValueError:
            flash("Ungültige Datei-IDs.", "error")
            return redirect(url_for("funde.list_funde"))
        if not file_ids:
            flash("Keine Dateien ausgewählt.", "warning")
            return redirect(url_for("funde.list_funde"))

        common = {
            "org_unit_id":             _int_or_none(request.form.get("org_unit_id")),
            "fachverantwortlicher_id": _int_or_none(request.form.get("fachverantwortlicher_id")),
            "idv_koordinator_id":      _int_or_none(request.form.get("idv_koordinator_id")),
            "entwicklungsart":         request.form.get("entwicklungsart", "arbeitshilfe"),
            "pruefintervall_monate":   _int_or_none(request.form.get("pruefintervall_monate")) or 12,
        }
        person_id = session.get("person_id")
        user_name = session.get("user_name", "")

        # Alle Formulardaten im Request-Kontext einlesen – der Writer-Thread
        # hat keinen Zugriff auf flask.request.
        per_file  = []
        error_fids = []
        for fid in file_ids:
            bez = (request.form.get(f"bezeichnung_{fid}") or "").strip()
            typ = (request.form.get(f"idv_typ_{fid}") or "unklassifiziert").strip()
            entw = _int_or_none(request.form.get(f"idv_entwickler_id_{fid}"))
            if not bez:
                error_fids.append(fid)
                continue
            per_file.append({
                "file_id":           fid,
                "bezeichnung":       bez,
                "idv_typ":           typ,
                # Fallback: Ersteller wird Entwickler, falls in der Zeile
                # nichts ausgewählt — sonst greift der Ownership-Check nicht.
                "idv_entwickler_id": entw or person_id,
            })

        created = []
        if per_file:
            try:
                def _do(c):
                    out = []
                    with write_tx(c):
                        for entry in per_file:
                            data = dict(common)
                            data.update(entry)
                            new_id = create_idv(c, data, erfasser_id=person_id,
                                                bearbeiter_name=user_name, commit=False)
                            out.append((entry["file_id"], new_id))
                    return out

                created = get_writer().submit(_do, wait=True) or []
            except Exception as exc:
                flash(f"Fehler beim Anlegen: {exc}", "error")
                # Alle als fehlgeschlagen behandeln, damit der Nutzer seine Eingaben behält
                error_fids = list(file_ids)

        # Partial-Success: Fehlgeschlagene Zeilen erneut anzeigen mit eingegebenen Werten
        if error_fids:
            if created:
                flash(
                    f"{len(created)} Eigenentwicklung(en) angelegt. "
                    f"{len(error_fids)} Zeile(n) fehlen noch – bitte Bezeichnung nachtragen.",
                    "warning",
                )
            else:
                flash("Bitte für alle Zeilen eine Bezeichnung eintragen.", "error")
            ph, ph_params = in_clause(error_fids)
            dateien = db.execute(
                f"SELECT * FROM idv_files WHERE id IN ({ph}) ORDER BY file_name",
                ph_params,
            ).fetchall()
            vorschlaege = []
            for d in dateien:
                vorschlaege.append({
                    "datei":             d,
                    "bezeichnung":       (request.form.get(f"bezeichnung_{d['id']}") or "").strip(),
                    "idv_typ":           (request.form.get(f"idv_typ_{d['id']}") or "unklassifiziert").strip(),
                    "idv_entwickler_id": _int_or_none(request.form.get(f"idv_entwickler_id_{d['id']}")),
                })
            cancel_url = _safe_referer_url(request, url_for("funde.list_funde"))
            return render_template("eigenentwicklung/bulk_neu.html",
                                   vorschlaege=vorschlaege,
                                   prefill_common=common,
                                   cancel_url=cancel_url,
                                   **_form_lookups(db))

        if created:
            flash(f"{len(created)} Eigenentwicklung(en) angelegt.", "success")
        return redirect(url_for("eigenentwicklung.list_idv"))

    # ── GET ──
    raw_ids = request.args.getlist("file_ids")
    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        file_ids = []
    if not file_ids:
        flash("Keine Dateien ausgewählt.", "warning")
        return redirect(url_for("funde.list_funde"))

    ph, ph_params = in_clause(file_ids)
    dateien = db.execute(
        f"SELECT * FROM idv_files WHERE id IN ({ph}) ORDER BY file_name",
        ph_params
    ).fetchall()

    # Pfad-Profil-Vorschlag: das auf die Mehrheit der selektierten Funde
    # passende Profil wird als Kopf-Vorbelegung gezogen (längstes Präfix gewinnt).
    profil_vorbelegung = _best_fund_pfad_profil(db, [d["full_path"] for d in dateien])

    # Pro Datei: Bezeichnungs-/Typ-/Entwickler-Vorschläge aufbereiten
    vorschlaege = []
    for d in dateien:
        ext = (d["extension"] or "").lower()
        name = d["file_name"] or ""
        if ext and name.lower().endswith(ext):
            name = name[:-len(ext)]
        typ = _idv_typ_vorschlag(ext, d["has_macros"])
        dev_id = None
        owner_hint = d["file_owner"] or d["office_author"] or ""
        if owner_hint:
            ad_login = owner_hint.split("\\")[-1] if "\\" in owner_hint else owner_hint
            dev_row = db.execute(
                """SELECT id FROM persons WHERE aktiv=1 AND (
                    user_id=? OR ad_name=?
                    OR (vorname || ' ' || nachname)=?
                    OR (nachname || ', ' || vorname)=?
                    OR (nachname || ' ' || vorname)=?
                ) LIMIT 1""",
                (ad_login, ad_login, owner_hint, owner_hint, owner_hint)
            ).fetchone()
            if dev_row:
                dev_id = dev_row["id"]
        vorschlaege.append({
            "datei":            d,
            "bezeichnung":      name,
            "idv_typ":          typ,
            "idv_entwickler_id": dev_id,
        })

    cancel_url = _safe_referer_url(request, url_for("funde.list_funde"))
    return render_template("eigenentwicklung/bulk_neu.html",
                           vorschlaege=vorschlaege,
                           profil_vorbelegung=profil_vorbelegung,
                           cancel_url=cancel_url,
                           **_form_lookups(db))



# ── Bearbeiten ─────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/bearbeiten", methods=["GET", "POST"])
@own_write_required
def edit_idv(idv_db_id):
    db  = get_db()
    # VULN-E: einheitlicher Ownership-Guard.
    ensure_can_write_idv(db, idv_db_id)
    idv = db.execute("""
        SELECT r.*,
          p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
          p_en.nachname || ', ' || p_en.vorname AS entwickler,
          ou.bezeichnung AS org_einheit,
          v.pruefstatus
        FROM idv_register r
        LEFT JOIN v_idv_uebersicht v ON v.idv_id = r.idv_id
        LEFT JOIN persons p_fv ON r.fachverantwortlicher_id = p_fv.id
        LEFT JOIN persons p_en ON r.idv_entwickler_id = p_en.id
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.id = ?
    """, (idv_db_id,)).fetchone()
    if not idv:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    # Freigegebene IDVs sind schreibgeschützt – nur Admin darf editieren.
    # Alle anderen Rollen müssen zuerst ein neues Freigabeverfahren
    # (Wiederfreigabe / Wesentliche Änderung / Patch) starten.
    if idv["status"] == "Freigegeben" and session.get("user_role") != ROLE_ADMIN:
        flash("Dieses IDV ist freigegeben. Änderungen sind erst nach Start eines "
              "neuen Freigabeverfahrens (Wiederfreigabe / Wesentliche Änderung / "
              "Patch) möglich.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    if request.method == "POST":
        data = _form_to_dict(request.form)
        person_id = session.get("person_id")
        user_name = session.get("user_name", "")
        try:
            antworten = _build_wesentlichkeit_answers(db, request.form)

            def _do(c):
                with write_tx(c):
                    update_idv(c, idv_db_id, data, geaendert_von_id=person_id,
                               bearbeiter_name=user_name, commit=False)
                    if antworten:
                        save_idv_wesentlichkeit(c, idv_db_id, antworten, commit=False)

            get_writer().submit(_do, wait=True)
            flash("Eigenentwicklung gespeichert.", "success")
            if request.form.get("save_action") == "save_and_close":
                return redirect(url_for("eigenentwicklung.list_idv"))
            return redirect(url_for("eigenentwicklung.edit_idv", idv_db_id=idv_db_id))
        except Exception as e:
            flash(f"Fehler: {e}", "error")

    # Vorhandene Kriterium-Antworten (inkl. angekreuzter Detail-IDs)
    wesentlichkeit_antworten = {}
    for row in get_idv_wesentlichkeit(db, idv_db_id):
        ant = dict(row)
        ant["detail_ids"] = [
            d["id"] for d in row.get("details") or [] if d.get("gewaehlt")
        ]
        wesentlichkeit_antworten[row["kriterium_id"]] = ant

    file = db.execute(
        "SELECT * FROM idv_files WHERE id=?", (idv["file_id"],)
    ).fetchone() if idv["file_id"] else None

    try:
        extra_files = db.execute("""
            SELECT f.*, lnk.id AS link_id, lnk.linked_at
            FROM idv_file_links lnk JOIN idv_files f ON f.id = lnk.file_id
            WHERE lnk.idv_db_id = ? ORDER BY lnk.linked_at
        """, (idv_db_id,)).fetchall()
    except Exception:
        extra_files = []

    _raw_history = db.execute("""
        SELECT h.*, COALESCE(p.nachname||', '||p.vorname, h.bearbeiter_name) AS person
        FROM idv_history h LEFT JOIN persons p ON h.durchgefuehrt_von_id = p.id
        WHERE h.idv_id = ? ORDER BY h.durchgefuehrt_am DESC LIMIT 50
    """, (idv_db_id,)).fetchall()
    _FELD_LABELS = {
        "bezeichnung":"Bezeichnung","version":"Version","idv_typ":"Typ",
        "entwicklungsart":"Art","status":"Status",
        "fachverantwortlicher_id":"Fachverantwortlicher",
        "idv_entwickler_id":"Entwickler","org_unit_id":"Org.-Einheit",
        "gp_id":"Geschäftsprozess","naechste_pruefung":"Nächste Prüfung",
        "pruefintervall_monate":"Prüfintervall","teststatus":"Teststatus",
    }
    history = []
    for h in _raw_history:
        row = dict(h)
        row["aenderungen_summary"] = ""
        if h["geaenderte_felder"]:
            try:
                chg = json.loads(h["geaenderte_felder"])
                row["aenderungen_summary"] = ", ".join(
                    _FELD_LABELS.get(k, k) for k in chg)
            except Exception:
                pass
        history.append(row)

    massnahmen = db.execute("""
        SELECT m.*, p.nachname||', '||p.vorname AS verantwortlicher,
               CASE WHEN m.faellig_am < date('now')
                         AND m.status IN ('Offen','In Bearbeitung')
                    THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m LEFT JOIN persons p ON m.verantwortlicher_id = p.id
        WHERE m.idv_id = ? ORDER BY m.faellig_am ASC
    """, (idv_db_id,)).fetchall()

    wesentlichkeit    = get_idv_wesentlichkeit(db, idv_db_id)
    ist_wesentlich    = any(k["erfuellt"] for k in wesentlichkeit)

    vorgaenger = None
    if idv["vorgaenger_idv_id"]:
        vorgaenger = db.execute(
            "SELECT id,idv_id,bezeichnung,version,status "
            "FROM idv_register WHERE id=?",
            (idv["vorgaenger_idv_id"],)
        ).fetchone()
    nachfolger = db.execute(
        """SELECT id,idv_id,bezeichnung,version,status,
                  letzte_aenderungsart,letzte_aenderungsbegruendung
           FROM idv_register WHERE vorgaenger_idv_id=?""",
        (idv_db_id,)
    ).fetchall()

    try:
        freigaben = db.execute("""
            SELECT f.*,
                   p_b.nachname||', '||p_b.vorname AS beauftragt_von,
                   p_d.nachname||', '||p_d.vorname AS durchgefuehrt_von,
                   p_z.nachname||', '||p_z.vorname AS zugewiesen_an,
                   pool.name AS pool_name
            FROM idv_freigaben f
            LEFT JOIN persons p_b ON f.beauftragt_von_id    = p_b.id
            LEFT JOIN persons p_d ON f.durchgefuehrt_von_id = p_d.id
            LEFT JOIN persons p_z ON f.zugewiesen_an_id     = p_z.id
            LEFT JOIN freigabe_pools pool ON f.pool_id      = pool.id
            WHERE f.idv_id = ? ORDER BY f.erstellt_am
        """, (idv_db_id,)).fetchall()
    except Exception:
        freigaben = []

    freigabe_persons = db.execute(
        "SELECT id,nachname,vorname,rolle FROM persons "
        "WHERE aktiv=1 ORDER BY nachname"
    ).fetchall()
    try:
        freigabe_pools = db.execute(
            "SELECT id,name FROM freigabe_pools WHERE aktiv=1 ORDER BY name"
        ).fetchall()
    except Exception:
        freigabe_pools = []

    _PHASE_1 = ["Fachlicher Test","Technischer Test"]
    _PHASE_2 = ["Fachliche Abnahme","Technische Abnahme"]
    _PHASE_3 = ["Archivierung Originaldatei"]
    phase1_schritte  = [f for f in freigaben if f["schritt"] in _PHASE_1]
    phase2_schritte  = [f for f in freigaben if f["schritt"] in _PHASE_2]
    phase3_schritte  = [f for f in freigaben if f["schritt"] in _PHASE_3]
    phase1_gestartet = len(phase1_schritte) > 0
    phase1_erledigt  = (
        {f["schritt"] for f in phase1_schritte if f["status"]=="Erledigt"}
        == set(_PHASE_1)
    )
    phase2_gestartet = len(phase2_schritte) > 0
    phase2_erledigt  = (
        {f["schritt"] for f in phase2_schritte if f["status"]=="Erledigt"}
        == set(_PHASE_2)
    )
    phase3_gestartet = len(phase3_schritte) > 0
    hat_offenen_schritt = any(
        f["status"] == "Ausstehend" and f["schritt"] not in _PHASE_3
        for f in freigaben
    )
    fachliche_testfaelle = get_fachliche_testfaelle(db, idv_db_id)
    technischer_test     = get_technischer_test(db, idv_db_id)
    fachlich_vorhanden   = bool(fachliche_testfaelle)
    technisch_vorhanden  = technischer_test is not None

    from .freigaben import _unprotected_excel_files_for_idv
    ungeschuetzte_excel = _unprotected_excel_files_for_idv(db, idv_db_id)

    return render_template("eigenentwicklung/form.html",
        idv=idv, fund=None, prefill={},
        wesentlichkeit_antworten=wesentlichkeit_antworten,
        can_write=can_write(), can_create=can_create(),
        file=file, extra_files=extra_files, history=history,
        massnahmen=massnahmen,
        wesentlichkeit=wesentlichkeit, ist_wesentlich=ist_wesentlich,
        vorgaenger=vorgaenger, nachfolger=nachfolger,
        freigaben=freigaben,
        freigabe_persons=freigabe_persons, freigabe_pools=freigabe_pools,
        phase1_gestartet=phase1_gestartet, phase1_erledigt=phase1_erledigt,
        phase2_gestartet=phase2_gestartet, phase2_erledigt=phase2_erledigt,
        phase3_gestartet=phase3_gestartet,
        hat_offenen_schritt=hat_offenen_schritt,
        teststatus_werte=_TESTSTATUS_WERTE,
        fachliche_testfaelle=fachliche_testfaelle,
        technischer_test=technischer_test,
        fachlich_vorhanden=fachlich_vorhanden,
        technisch_vorhanden=technisch_vorhanden,
        entwicklungsart_label=ENTWICKLUNGSART_LABEL,
        ungeschuetzte_excel=ungeschuetzte_excel,
        **_form_lookups(db))


# ── Status ─────────────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/status", methods=["POST"])
@write_access_required
def change_status_route(idv_db_id):
    # ``@write_access_required`` erlaubt nur Admin/Koordinator – dort reicht
    # die rollenbasierte Prüfung aus. Der explizite Ownership-Check schadet
    # nicht und ist konsistent mit anderen schreibenden Routen (VULN-E).
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    new_status = request.form.get("status")
    person_id  = session.get("person_id")
    user_name  = session.get("user_name", "")
    if new_status:
        get_writer().submit(
            lambda c: change_status(c, idv_db_id, new_status,
                                    geaendert_von_id=person_id,
                                    bearbeiter_name=user_name),
            wait=True,
        )
        flash(f"Status geändert zu: {new_status}", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


_TESTSTATUS_WERTE = [
    "Wertung ausstehend", "In Bearbeitung", "Freigabe ausstehend", "Freigegeben"
]


@bp.route("/<int:idv_db_id>/teststatus", methods=["POST"])
@own_write_required
def change_teststatus(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    val = request.form.get("teststatus", "")
    if val not in _TESTSTATUS_WERTE:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(ok=False, error="Ungültiger Teststatus."), 400
        flash("Ungültiger Teststatus.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
    person_id = session.get("person_id")
    user_name = session.get("user_name", "")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_register SET teststatus=?, aktualisiert_am=? WHERE id=?",
                (val, now, idv_db_id),
            )
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "teststatus_geaendert", f"Teststatus → {val}", person_id, user_name or None),
            )

    get_writer().submit(_do, wait=True)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True, teststatus=val)
    flash(f"Teststatus geändert zu: {val}", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:idv_db_id>/datei-verknuepfung/<int:link_id>/loeschen", methods=["POST"])
@own_write_required
def unlink_file(idv_db_id, link_id):
    """Entfernt eine zusätzliche Datei-Verknüpfung (idv_file_links)."""
    db = get_db()
    ensure_can_write_idv(db, idv_db_id)
    row = db.execute(
        "SELECT lnk.*, f.file_name FROM idv_file_links lnk JOIN idv_files f ON f.id=lnk.file_id WHERE lnk.id=? AND lnk.idv_db_id=?",
        (link_id, idv_db_id)
    ).fetchone()
    if not row:
        flash("Verknüpfung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    file_id_ref = row["file_id"]

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM idv_file_links WHERE id=?", (link_id,))
            c.execute(
                "UPDATE idv_files SET bearbeitungsstatus='Neu' WHERE id=? AND bearbeitungsstatus='Registriert'",
                (file_id_ref,),
            )

    get_writer().submit(_do, wait=True)
    flash(f"Verknüpfung mit \"{row['file_name']}\" aufgehoben.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:idv_db_id>/dateien-verknuepfen", methods=["GET", "POST"])
@own_write_required
def link_files(idv_db_id):
    """Direkte Datei-Verknüpfung: zeigt freie Scanner-Funde und verknüpft ausgewählte mit der Eigenentwicklung."""
    db = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    if request.method == "POST":
        raw_ids = request.form.getlist("file_ids")
        try:
            file_ids = [int(i) for i in raw_ids if i]
        except ValueError:
            flash("Ungültige Datei-IDs.", "error")
            return redirect(url_for("eigenentwicklung.link_files", idv_db_id=idv_db_id))

        if not file_ids:
            flash("Keine Dateien ausgewählt.", "warning")
            return redirect(url_for("eigenentwicklung.link_files", idv_db_id=idv_db_id))

        def _do(c):
            ok = 0
            with write_tx(c):
                for fid in file_ids:
                    try:
                        c.execute(
                            "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                            (idv_db_id, fid),
                        )
                        c.execute(
                            "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id=?",
                            (fid,),
                        )
                        ok += 1
                    except Exception:
                        pass
            return ok

        linked = get_writer().submit(_do, wait=True)
        flash(f"{linked} Datei(en) mit Eigenentwicklung {idv['idv_id']} verknüpft.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    # GET – Vorschläge berechnen (Hash + Ähnlichkeit) und Gesamtanzahl ermitteln
    from .. import similarity as _sim
    sim_cfg   = _sim.get_config(db)
    sim_noise = frozenset(sim_cfg["noise_words"])

    # 1. Hash-basierte Vorschläge: Dateien mit gleichem SHA-256-Hash wie
    #    eine bereits verknüpfte Datei dieser Eigenentwicklung
    linked_hash_rows = db.execute("""
        SELECT DISTINCT f.file_hash
        FROM idv_files f
        WHERE f.file_hash IS NOT NULL AND f.file_hash != '' AND f.file_hash != 'HASH_ERROR'
          AND (
              f.id = COALESCE((SELECT file_id FROM idv_register WHERE id = ?), -1)
              OR f.id IN (SELECT file_id FROM idv_file_links WHERE idv_db_id = ?)
          )
    """, (idv_db_id, idv_db_id)).fetchall()
    known_hashes = [r['file_hash'] for r in linked_hash_rows]

    hash_vorschlaege = []
    hash_vorschlag_ids = set()
    if known_hashes:
        ph_h, ph_h_params = in_clause(known_hashes)
        hash_rows = db.execute(f"""
            SELECT f.id, f.file_name, f.extension, f.has_macros, f.share_root,
                   f.relative_path, f.full_path, f.size_bytes, f.modified_at, f.file_hash
            FROM idv_files f
            WHERE f.status = 'active'
              AND f.file_hash IN ({ph_h})
              AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)
              AND f.id != COALESCE((SELECT file_id FROM idv_register WHERE id = ?), -1)
            ORDER BY f.file_name
        """, ph_h_params + [idv_db_id]).fetchall()
        hash_vorschlaege = [dict(r) for r in hash_rows]
        hash_vorschlag_ids = {r['id'] for r in hash_rows}

    # 2. Ähnlichkeits-basierte Vorschläge: Scoring zentral in webapp/similarity.py
    idv_typ = idv['idv_typ'] or ''
    dev_rows = []
    for pid in filter(None, [idv['idv_entwickler_id'], idv['fachverantwortlicher_id']]):
        p = db.execute(
            "SELECT user_id, ad_name FROM persons WHERE id = ?", (pid,)
        ).fetchone()
        if p:
            dev_rows.append(p)
    dev_ids_lower = _sim.collect_dev_ids(dev_rows)

    kandidaten = db.execute(f"""
        SELECT f.id, f.file_name, f.extension, f.has_macros, f.share_root,
               f.relative_path, f.full_path, f.size_bytes, f.modified_at, f.file_owner
        FROM idv_files f
        WHERE f.status = 'active'
          AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)
          AND f.id != COALESCE((SELECT file_id FROM idv_register WHERE id = ?), -1)
        ORDER BY f.last_seen_at DESC
        LIMIT {int(sim_cfg['max_candidates'])}
    """, (idv_db_id,)).fetchall()

    idv_name = idv['bezeichnung'] or ''
    aehnlichkeit_vorschlaege = []
    for kand in kandidaten:
        if kand['id'] in hash_vorschlag_ids:
            continue
        fund_typ = _idv_typ_vorschlag(kand['extension'], kand['has_macros'])
        score = _sim.score_pair(
            fund_typ=fund_typ,
            fund_owner=kand['file_owner'] or '',
            fund_name=kand['file_name'] or '',
            idv_typ=idv_typ,
            idv_name=idv_name,
            dev_ids_lower=dev_ids_lower,
            config=sim_cfg,
            noise=sim_noise,
        )
        if score >= sim_cfg['threshold']:
            row = dict(kand)
            row['score'] = score
            aehnlichkeit_vorschlaege.append(row)

    aehnlichkeit_vorschlaege.sort(key=lambda x: x['score'], reverse=True)
    aehnlichkeit_vorschlaege = aehnlichkeit_vorschlaege[:sim_cfg['max_results']]

    total_count = db.execute("""
        SELECT COUNT(*)
        FROM idv_files f
        WHERE f.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id
          )
          AND f.id != COALESCE((SELECT file_id FROM idv_register WHERE id = ?), -1)
    """, (idv_db_id,)).fetchone()[0]

    return render_template(
        "eigenentwicklung/datei_verknuepfen.html",
        idv=idv,
        total_count=total_count,
        hash_vorschlaege=hash_vorschlaege,
        aehnlichkeit_vorschlaege=aehnlichkeit_vorschlaege,
    )


@bp.route("/<int:idv_db_id>/dateien-suchen")
@own_write_required
def link_files_search(idv_db_id):
    """AJAX-Endpoint: freie Scanner-Funde suchen und paginiert zurückgeben."""
    db = get_db()
    ensure_can_write_idv(db, idv_db_id)
    if not db.execute("SELECT 1 FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone():
        return jsonify({"error": "Eigenentwicklung nicht gefunden"}), 404

    q = request.args.get("q", "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 50, 0

    base_where = """
        FROM idv_files f
        WHERE f.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id
          )
          AND f.id != COALESCE((SELECT file_id FROM idv_register WHERE id = ?), -1)
    """
    params: list = [idv_db_id]

    if q:
        base_where += " AND (f.file_name LIKE ? OR f.full_path LIKE ?)"
        like = f"%{q}%"
        params += [like, like]

    total = db.execute(f"SELECT COUNT(*) {base_where}", params).fetchone()[0]

    rows = db.execute(
        f"""SELECT id, file_name, extension, has_macros, share_root,
                   relative_path, full_path, size_bytes, modified_at
            {base_where}
            ORDER BY f.last_seen_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    return jsonify({"total": total, "files": [dict(r) for r in rows]})


# ── Neue Version ───────────────────────────────────────────────────────────

@bp.route("/<int:idv_db_id>/neue-version", methods=["POST"])
@own_write_required
def neue_version(idv_db_id):
    """Erstellt eine neue Version einer Eigenentwicklung (Nachfolger-Dokument).

    Bei ``aenderungsart == 'unwesentlich'`` kann optional eine neue Dateiversion
    hochgeladen werden. Diese wird – analog zu Phase 3 des Freigabeverfahrens –
    revisionssicher im Archiv-Ordner abgelegt (SHA-256, schreibgeschützt) und
    als Hauptdatei (``file_id``) der neuen Version verknüpft. Ohne diesen Pfad
    würde bei unwesentlichen Änderungen überhaupt keine Dateifassung revisions-
    sicher gesichert, weil das Freigabeverfahren (inkl. Phase 3) übersprungen
    wird (vgl. ``freigaben._testverfahren_erforderlich``).
    """
    import hashlib
    import tempfile
    from datetime import datetime, timezone
    from flask import current_app
    from werkzeug.utils import secure_filename

    _MAX_ARCHIV_UPLOAD = 256 * 1024 * 1024  # identisch zu freigaben._MAX_ARCHIV_UPLOAD

    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    src = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not src:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

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

    # Optionaler Datei-Upload für unwesentliche Änderungen: streamen in eine
    # Temp-Datei (damit die 256-MB-Grenze greift, bevor irgendetwas in der DB
    # landet) und SHA-256 on-the-fly berechnen. Das endgültige Verschieben in
    # den Archiv-Ordner passiert unten, sobald ``new_id`` bekannt ist.
    archiv_meta = None
    if aenderungsart == "unwesentlich":
        upload_file = request.files.get("archiv_datei")
        if upload_file and upload_file.filename:
            original_name = upload_file.filename
            try:
                upload_file.stream.seek(0)
            except Exception:
                pass
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="idv_nv_", suffix=".bin")
            h = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(tmp_fd, "wb") as out:
                    while True:
                        chunk = upload_file.stream.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_ARCHIV_UPLOAD:
                            out.close()
                            try: os.remove(tmp_path)
                            except OSError: pass
                            flash(
                                "Archiv-Upload abgelehnt: Datei ist größer als "
                                f"{_MAX_ARCHIV_UPLOAD // (1024 * 1024)} MB.",
                                "error",
                            )
                            return redirect(
                                url_for("eigenentwicklung.detail_idv",
                                        idv_db_id=idv_db_id))
                        out.write(chunk)
                        h.update(chunk)
            except OSError as exc:
                try: os.remove(tmp_path)
                except OSError: pass
                current_app.logger.warning(
                    "Archiv-Upload (Neue Version, IDV %s) fehlgeschlagen: %s",
                    idv_db_id, exc,
                )
                flash("Archiv-Upload fehlgeschlagen (Dateisystem-Fehler).", "error")
                return redirect(url_for("eigenentwicklung.detail_idv",
                                        idv_db_id=idv_db_id))
            safe_name = secure_filename(original_name) or "original.bin"
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_")
            ext = original_name.rsplit(".", 1)[1].lower() if "." in original_name else ""
            archiv_meta = {
                "tmp_path": tmp_path,
                "save_name": timestamp_str + safe_name,
                "name": original_name,
                "sha256": h.hexdigest(),
                "size": total,
                "ext": ext,
            }

    # Archiv-Basisordner vorab auflösen – ``current_app.instance_path`` darf
    # nur im Request-Thread angefasst werden, nicht aus dem Writer-Thread.
    archiv_base_dir = os.path.join(current_app.instance_path, "uploads", "archiv")

    # Daten aus Quell-IDV kopieren
    data = dict(src)
    data["vorgaenger_idv_id"]             = idv_db_id
    data["version"]                       = new_version
    data["teststatus"]                    = "Wertung ausstehend"
    data["letzte_aenderungsart"]          = aenderungsart
    data["letzte_aenderungsbegruendung"]  = aenderungsbegruendung
    # Felder entfernen, die create_idv selbst setzt oder die versionsexklusiv sind.
    # file_id und weitere_dateien werden NICHT kopiert: jede Version muss ihre eigene
    # Datei explizit verknüpfen, damit die Dateihistorie je Version nachvollziehbar ist.
    # freigabe_aenderungskategorie / freigabe_patch_begruendung gehören zum
    # Freigabeverfahren der Vorgänger-Version (#320) – die neue Version startet
    # ohne Einstufung, damit „Freigabeverfahren starten" wieder angeboten wird.
    for k in ("id", "idv_id", "status", "status_geaendert_am", "status_geaendert_von_id",
              "erstellt_am", "aktualisiert_am", "erfasst_von_id",
              "naechste_pruefung", "letzte_pruefung",
              "file_id", "weitere_dateien",
              "freigabe_aenderungskategorie", "freigabe_patch_begruendung"):
        data.pop(k, None)

    person_id = session.get("person_id")
    user_name = session.get("user_name", "")

    # Wesentlichkeitskriterien-Antworten aus Quelle schon jetzt lesen.
    quell_antworten = get_idv_wesentlichkeit(db, idv_db_id)
    kopierte_antworten = [
        {"kriterium_id": a["kriterium_id"], "erfuellt": a["erfuellt"],
         "begruendung": a["begruendung"]}
        for a in quell_antworten
    ]

    aenderung_info = ""
    if aenderungsart:
        aenderung_info = f" | Änderungsart: {aenderungsart}"
        if aenderungsbegruendung:
            aenderung_info += f" – {aenderungsbegruendung}"

    def _do(c):
        with write_tx(c):
            new_id = create_idv(c, data, erfasser_id=person_id,
                                bearbeiter_name=user_name, commit=False)
            if kopierte_antworten:
                save_idv_wesentlichkeit(c, new_id, kopierte_antworten, commit=False)
            new_idv_row = c.execute(
                "SELECT idv_id FROM idv_register WHERE id=?", (new_id,)
            ).fetchone()
            c.execute(
                "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                (idv_db_id, "neue_version",
                 f"Nachfolger {new_idv_row['idv_id']} (v{new_version}) angelegt{aenderung_info}",
                 person_id, user_name or None),
            )

            if archiv_meta is not None:
                archiv_folder = os.path.join(archiv_base_dir, str(new_id))
                os.makedirs(archiv_folder, exist_ok=True)
                archiv_dest = os.path.join(archiv_folder, archiv_meta["save_name"])
                os.rename(archiv_meta["tmp_path"], archiv_dest)
                try:
                    os.chmod(archiv_dest, 0o444)
                except OSError:
                    pass
                now_iso = datetime.now(timezone.utc).isoformat()
                cur = c.execute(
                    "INSERT INTO idv_files ("
                    "  file_hash, full_path, file_name, extension, size_bytes, "
                    "  source, status, bearbeitungsstatus, "
                    "  first_seen_at, last_seen_at"
                    ") VALUES (?, ?, ?, ?, ?, 'filesystem', 'active', 'Registriert', ?, ?)",
                    (archiv_meta["sha256"], archiv_dest, archiv_meta["name"],
                     archiv_meta["ext"], archiv_meta["size"], now_iso, now_iso),
                )
                new_file_id = cur.lastrowid
                c.execute(
                    "UPDATE idv_register SET file_id=? WHERE id=?",
                    (new_file_id, new_id),
                )
                c.execute(
                    "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) VALUES (?,?,?,?,?)",
                    (new_id, "archivierung_unwesentliche_aenderung",
                     f"Dateiversion '{archiv_meta['name']}' revisionssicher "
                     f"archiviert (SHA-256: {archiv_meta['sha256']})",
                     person_id, user_name or None),
                )
        return new_id

    try:
        new_id = get_writer().submit(_do, wait=True)
        if archiv_meta is not None:
            flash(
                f"Neue Version {new_version} angelegt; Dateiversion archiviert "
                f"(SHA-256: {archiv_meta['sha256'][:12]}…).",
                "success",
            )
        else:
            flash(f"Neue Version {new_version} angelegt.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=new_id))
    except Exception as e:
        # Temp-Datei aufräumen, falls _do vor dem rename fehlgeschlagen ist.
        if archiv_meta is not None:
            try: os.remove(archiv_meta["tmp_path"])
            except OSError: pass
        flash(f"Fehler beim Anlegen der neuen Version: {e}", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


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
                               "scanner", "excel_export.py")
        result = subprocess.run(
            [sys.executable, script, "--db", db_path, "--output", out_path],
            check=True, capture_output=True, text=True
        )
        return send_file(out_path, as_attachment=True,
                         download_name="Eigenentwicklungen_Grundgesamtheit.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        msg = detail[-1] if detail else str(e)
        flash(f"Export fehlgeschlagen: {msg}", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))
    except Exception as e:
        flash(f"Export fehlgeschlagen: {e}", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))


# ── Hilfsfunktion: Formular → Dict ─────────────────────────────────────────

def _form_to_dict(form) -> dict:
    def chk(k): return 1 if form.get(k) == "1" else 0

    return {
        "bezeichnung":               form.get("bezeichnung", "").strip(),
        "kurzbeschreibung":          form.get("kurzbeschreibung", "").strip() or None,
        "version":                   form.get("version", "1.0").strip(),
        "idv_typ":                   form.get("idv_typ", "unklassifiziert"),
        "entwicklungsart":           form.get("entwicklungsart", "arbeitshilfe"),
        "gp_id":                     _int_or_none(form.get("gp_id")),
        "gp_freitext":               form.get("gp_freitext") or None,
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
        "anwenderdokumentation":     chk("anwenderdokumentation"),
        "datenschutz_beachtet":      chk("datenschutz_beachtet"),
        "zellschutz_formeln":        chk("zellschutz_formeln"),
        "datenschutz_kategorie":     form.get("datenschutz_kategorie") or "keine",
        "produktiv_seit":            form.get("produktiv_seit") or None,
        "nutzungsfrequenz":          form.get("nutzungsfrequenz") or None,
        "nutzeranzahl":              _int_or_none(form.get("nutzeranzahl")),
        "datenquellen":              form.get("datenquellen") or None,
        "datenempfaenger":           form.get("datenempfaenger") or None,
        "dokumentation_vorhanden":   chk("dokumentation_vorhanden"),
        "testkonzept_vorhanden":     chk("testkonzept_vorhanden"),
        "versionskontrolle":         chk("versionskontrolle"),
        "pruefintervall_monate":     _int_or_none(form.get("pruefintervall_monate")) or 12,
        "abloesung_geplant":         chk("abloesung_geplant"),
        "abloesung_zieldatum":       form.get("abloesung_zieldatum") or None,
        "abloesung_durch":           form.get("abloesung_durch") or None,
        "interne_notizen":           form.get("interne_notizen") or None,
        # Neue Felder (außerhalb Wesentlichkeitsbeurteilung)
        "erstellt_fuer":             form.get("erstellt_fuer") or None,
        "schnittstellen_beschr":     form.get("schnittstellen_beschr") or None,
    }


def _build_wesentlichkeit_answers(db, form) -> list:
    """Read-only: erzeugt die Antwortliste aus dem Formular.

    Gibt die Liste der Kriterium-Antworten zurueck, aber schreibt nicht.
    Muss vor einem writer.submit() auf der Reader-Connection aufgerufen
    werden.
    """
    criteria = db.execute(
        "SELECT id FROM wesentlichkeitskriterien WHERE aktiv=1"
    ).fetchall()
    antworten = []
    for k in criteria:
        kid = k["id"]
        detail_ids = []
        for raw in form.getlist(f"kriterium_detail_{kid}"):
            try:
                detail_ids.append(int(raw))
            except (ValueError, TypeError):
                continue
        antworten.append({
            "kriterium_id": kid,
            "erfuellt":     1 if form.get(f"kriterium_{kid}") == "1" else 0,
            "begruendung":  form.get(f"kriterium_begr_{kid}") or None,
            "detail_ids":   detail_ids,
        })
    return antworten


def _save_wesentlichkeit_from_form(db, idv_db_id: int, form) -> None:
    """Bewahrt die alte Signatur fuer Aufrufer, die keinen eigenen Writer-
    Closure brauchen: baut die Antworten auf der Reader-Connection und
    schreibt sie ueber den Writer-Thread."""
    antworten = _build_wesentlichkeit_answers(db, form)
    if antworten:
        get_writer().submit(
            lambda c: save_idv_wesentlichkeit(c, idv_db_id, antworten),
            wait=True,
        )


# ── Nicht-wesentliche Eigenentwicklungen ──────────────────────────────────

@bp.route("/nicht-wesentlich")
@login_required
def nicht_wesentliche_idvs():
    """Eigene Seite: Nicht-wesentliche Eigenentwicklungen aus dem Register."""
    db = get_db()
    q          = request.args.get("q", "").strip()
    share_root = request.args.get("share_root", "").strip()
    status     = request.args.get("status", "")
    oe_id      = _int_or_none(request.args.get("oe_id"))
    fv_id      = _int_or_none(request.args.get("fv_id"))
    owner_filt = request.args.get("owner", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE_IDV:
            session["pref_per_page_nw"] = per_page
    else:
        per_page = session.get("pref_per_page_nw", 100)
    if per_page not in _VALID_PER_PAGE_IDV:
        per_page = 100

    _WESENTLICH = """EXISTS(
        SELECT 1 FROM idv_wesentlichkeit iw
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
    )"""

    where_parts = [f"NOT {_WESENTLICH}", "v.status != 'Archiviert'"]
    params: list = []

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
    if owner_filt:
        where_parts.append(
            "r.file_id IN (SELECT id FROM idv_files WHERE file_owner = ?)"
        )
        params.append(owner_filt)

    where_sql = "WHERE " + " AND ".join(where_parts)

    total = db.execute(
        f"""SELECT COUNT(*) FROM v_idv_uebersicht v
            JOIN idv_register r ON r.idv_id = v.idv_id
            {where_sql}""",
        params,
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    _NW_WESENTLICH = _WESENTLICH
    nicht_wesentliche = db.execute(f"""
        SELECT r.*, v.*,
          CASE WHEN {_NW_WESENTLICH} THEN 1 ELSE 0 END AS ist_wesentlich,
          EXISTS(SELECT 1 FROM idv_register x WHERE x.vorgaenger_idv_id = r.id) AS hat_nachfolger,
          (CASE WHEN r.file_id IS NOT NULL THEN 1 ELSE 0 END
           + (SELECT COUNT(*) FROM idv_file_links lnk WHERE lnk.idv_db_id = r.id)) AS datei_anzahl,
          f.formula_count        AS file_formula_count,
          f.has_macros           AS file_has_macros,
          f.has_sheet_protection AS file_has_sheet_protection,
          f.file_owner           AS file_owner
        FROM v_idv_uebersicht v
        JOIN idv_register r ON r.idv_id = v.idv_id
        LEFT JOIN idv_files f ON f.id = r.file_id
        {where_sql}
        ORDER BY v.bezeichnung
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()

    org_units = db.execute(
        "SELECT id, bezeichnung FROM org_units WHERE aktiv=1 ORDER BY bezeichnung"
    ).fetchall()
    persons_fv = db.execute(
        "SELECT DISTINCT p.id, p.nachname, p.vorname FROM persons p"
        " WHERE p.aktiv=1"
        " AND EXISTS ("
        "   SELECT 1 FROM idv_register r"
        "   JOIN v_idv_uebersicht v ON v.idv_id = r.idv_id"
        f"  WHERE r.fachverantwortlicher_id = p.id AND NOT {_WESENTLICH}"
        " )"
        " ORDER BY p.nachname"
    ).fetchall()
    share_roots = [
        r["share_root"] for r in db.execute(
            "SELECT DISTINCT share_root FROM idv_files WHERE share_root IS NOT NULL AND status='active' ORDER BY share_root"
        ).fetchall()
    ]
    owner_list = [
        r["file_owner"] for r in db.execute(
            "SELECT DISTINCT file_owner FROM idv_files"
            " WHERE file_owner IS NOT NULL AND file_owner != '' AND status='active'"
            " ORDER BY file_owner"
        ).fetchall()
    ]

    from . import ROLE_ADMIN
    is_admin = (session.get("user_role") == ROLE_ADMIN)
    return render_template("eigenentwicklung/nicht_wesentlich.html",
        nicht_wesentliche=nicht_wesentliche,
        total=total, total_pages=total_pages, page=page, per_page=per_page,
        org_units=org_units, persons_fv=persons_fv,
        share_roots=share_roots, share_root=share_root,
        owner_list=owner_list, owner_filt=owner_filt,
        q=q, status=status, oe_id=oe_id, fv_id=fv_id,
        valid_per_page=_VALID_PER_PAGE_IDV,
        can_write=can_write(),
        is_admin=is_admin,
    )
