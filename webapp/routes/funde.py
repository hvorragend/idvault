"""Funde-Blueprint (Scanner-Ergebnisse)"""
import json
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session
from . import login_required, write_access_required, own_write_required, get_db, admin_required, current_user_role, ROLE_ADMIN, can_write
from ..security import in_clause

bp = Blueprint("funde", __name__, url_prefix="/funde")


@bp.record_once
def _bootstrap_extras(state):
    """Registriert safe_url_for als Jinja2-Global (verhindert BuildError in base.html)."""
    from werkzeug.routing import BuildError

    def _safe_url_for(endpoint, **values):
        try:
            from flask import url_for as _url_for
            return _url_for(endpoint, **values)
        except BuildError:
            return "#"

    state.app.jinja_env.globals.setdefault("safe_url_for", _safe_url_for)
    state.app.jinja_env.filters.setdefault("map_path", lambda v: str(v) if v else "")


def _scan_btn_ctx() -> dict:
    """Liefert die Variablen für das _scan_button.html-Include."""
    from webapp.routes.admin import (
        _scan_is_running, _load_scanner_config, _scan_is_paused, _has_checkpoint
    )
    running = _scan_is_running()
    return {
        "can_write":      can_write(),
        "scan_running":   running,
        "scan_paused":    _scan_is_paused() if running else False,
        "has_scan_paths": bool(_load_scanner_config().get("scan_paths")),
        "has_checkpoint": _has_checkpoint(),
    }

_EXT_TO_TYP = {
    ".xlsx": "Excel-Tabelle",
    ".xlsm": "Excel-Makro",
    ".xlsb": "Excel-Makro",
    ".xls":  "Excel-Tabelle",
    ".xltm": "Excel-Makro",
    ".xltx": "Excel-Tabelle",
    ".accdb": "Access-Datenbank",
    ".mdb":   "Access-Datenbank",
    ".accde": "Access-Datenbank",
    ".accdr": "Access-Datenbank",
    ".py":    "Python-Skript",
    ".r":     "Sonstige",
    ".rmd":   "Sonstige",
    ".sql":   "SQL-Skript",
    ".pbix":  "Power-BI-Bericht",
    ".pbit":  "Power-BI-Bericht",
    ".ida":   "Cognos-Report",
}


def _idv_typ_vorschlag(extension: str, has_macros: int) -> str:
    ext = (extension or "").lower()
    if ext in (".xlsx", ".xls", ".xltx") and has_macros:
        return "Excel-Makro"
    return _EXT_TO_TYP.get(ext, "unklassifiziert")


def _scan_run_label(row) -> str:
    """Lesbare Kurzbezeichnung eines Scan-Laufs."""
    if not row:
        return "–"
    try:
        paths = json.loads(row["scan_paths"] or "[]")
    except Exception:
        paths = []
    datum = (row["started_at"] or "")[:16].replace("T", " ")
    pfad  = paths[0] if paths else "?"
    if len(paths) > 1:
        pfad += f" (+{len(paths)-1})"
    return f"#{row['id']} · {datum} · {pfad}"


_DIR_PATH_EXPR = """CASE WHEN f.file_name IS NOT NULL AND f.full_path IS NOT NULL
                         AND LENGTH(f.full_path) > LENGTH(f.file_name)
                    THEN SUBSTR(f.full_path, 1, LENGTH(f.full_path) - LENGTH(f.file_name) - 1)
                    ELSE f.share_root END"""

_DIR_PATH_EXPR_PLAIN = """CASE WHEN file_name IS NOT NULL AND full_path IS NOT NULL
                               AND LENGTH(full_path) > LENGTH(file_name)
                          THEN SUBSTR(full_path, 1, LENGTH(full_path) - LENGTH(file_name) - 1)
                          ELSE share_root END"""


_VALID_PER_PAGE = (25, 50, 100, 200, 500)


_FUNDE_SORT_COLS = {
    "dateiname":  "f.file_name",
    "groesse":    "f.size_bytes",
    "geaendert":  "f.modified_at",
    "scan":       "f.last_seen_at",
    "eigentümer": "f.file_owner",
    "dir_path":   _DIR_PATH_EXPR,
}


@bp.route("/")
@login_required
def list_funde():
    db          = get_db()
    filt        = request.args.get("filter", "")
    share_root  = request.args.get("share_root", "").strip()
    dir_path_filt = request.args.get("dir_path", "").strip()
    scan_run_id = request.args.get("scan_run", "").strip()
    q_search    = request.args.get("q", "").strip()
    sort        = request.args.get("sort", "scan").strip()
    order       = request.args.get("order", "desc").strip()
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE:
            session["pref_per_page_funde_list"] = per_page
    else:
        per_page = session.get("pref_per_page_funde_list", 100)
    if per_page not in _VALID_PER_PAGE:
        per_page = 100
    offset = (page - 1) * per_page

    # ---------- WHERE-Bedingungen ----------
    where_parts = []
    params      = []

    if scan_run_id:
        # Für einen konkreten Scan-Lauf: alle Dateien zeigen, nicht nur aktive
        try:
            where_parts.append("f.last_scan_run_id = ?")
            params.append(int(scan_run_id))
        except ValueError:
            scan_run_id = ""
    elif filt == "archiv":
        where_parts.append("f.status = 'archiviert'")
    elif filt == "duplikate":
        where_parts.append("f.status = 'active'")
        where_parts.append("""f.file_hash IN (
            SELECT file_hash FROM idv_files
            WHERE status='active' AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        )""")
    else:
        where_parts.append("f.status = 'active'")
        _no_idv = (
            "NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            " AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)"
        )
        _has_idv = (
            "(EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            " OR EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id))"
        )
        if filt == "ohne_idv":
            where_parts.append(_no_idv)
            where_parts.append("(f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')")
        elif filt == "mit_idv":
            where_parts.append(_has_idv)
        elif filt == "makros":
            where_parts.append("f.has_macros = 1")
        elif filt == "blattschutz":
            where_parts.append("f.has_sheet_protection = 1")
        elif filt == "ignoriert":
            where_parts.append("f.bearbeitungsstatus = 'Ignoriert'")
        elif filt == "zur_registrierung":
            where_parts.append("f.bearbeitungsstatus = 'Zur Registrierung'")
        else:
            # Standard-Ansicht "Alle": als Ignoriert bewertete Dateien ausblenden
            where_parts.append("(f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')")

    if share_root:
        where_parts.append("f.share_root = ?")
        params.append(share_root)

    if dir_path_filt:
        # Exakter Treffer ODER Unterverzeichnisse (beide Pfadtrennzeichen unterstützen)
        where_parts.append(
            f"({_DIR_PATH_EXPR} = ? OR {_DIR_PATH_EXPR} LIKE ? OR {_DIR_PATH_EXPR} LIKE ?)"
        )
        params.extend([dir_path_filt,
                        dir_path_filt + "\\%",
                        dir_path_filt + "/%"])

    if q_search:
        where_parts.append("f.file_name LIKE ?")
        params.append(f"%{q_search}%")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    # Duplikate nach Hash sortieren, damit Jinja2-groupby funktioniert
    if filt == "duplikate":
        order_sql = "ORDER BY f.file_hash, f.last_seen_at DESC"
    elif sort in _FUNDE_SORT_COLS:
        sort_col = _FUNDE_SORT_COLS[sort]
        sort_dir = "DESC" if order == "desc" else "ASC"
        order_sql = f"ORDER BY {sort_col} {sort_dir}, f.last_seen_at DESC"
    else:
        order_sql = "ORDER BY f.last_seen_at DESC, f.modified_at DESC"

    # Gesamtzahl für Pagination (Duplikate: Anzahl der eindeutigen Hashes)
    if filt == "duplikate":
        total = db.execute(f"""
            SELECT COUNT(DISTINCT f.file_hash) FROM idv_files f {where_sql}
        """, params).fetchone()[0]
    else:
        total = db.execute(
            f"SELECT COUNT(*) FROM idv_files f {where_sql}", params
        ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    dateien = db.execute(f"""
        SELECT f.*,
               {_DIR_PATH_EXPR} AS dir_path,
               COALESCE(reg.idv_id,       lnk_reg.idv_id)      AS reg_idv_id,
               COALESCE(reg.bezeichnung,  lnk_reg.bezeichnung)  AS reg_bezeichnung,
               COALESCE(reg.id,           lnk_reg.id)           AS reg_db_id,
               sr.id            AS sr_id,
               sr.started_at    AS sr_started_at,
               sr.scan_paths    AS sr_scan_paths
        FROM idv_files f
        LEFT JOIN idv_register  reg     ON reg.file_id    = f.id
        LEFT JOIN idv_file_links lnk    ON lnk.file_id    = f.id
        LEFT JOIN idv_register  lnk_reg ON lnk_reg.id     = lnk.idv_db_id
        LEFT JOIN scan_runs     sr      ON f.last_scan_run_id = sr.id
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()

    # ---------- Duplikat-Erkennung (datenbankweit, nicht nur in der aktuellen Seite) ----------
    duplicate_hashes = {
        r["file_hash"] for r in db.execute("""
            SELECT file_hash FROM idv_files
            WHERE status = 'active'
              AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        """).fetchall()
    }

    # ---------- Zählkarten ----------
    gesamt_inkl_ignoriert = db.execute("SELECT COUNT(*) FROM idv_files WHERE status='active'").fetchone()[0]
    ohne_idv   = db.execute("""
        SELECT COUNT(*) FROM idv_files f WHERE f.status='active'
        AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
        AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)
        AND (f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')
    """).fetchone()[0]
    mit_makro  = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_macros=1"
    ).fetchone()[0]
    mit_schutz = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_sheet_protection=1"
    ).fetchone()[0]
    archiviert = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='archiviert'"
    ).fetchone()[0]
    try:
        ignoriert = db.execute(
            "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Ignoriert'"
        ).fetchone()[0]
        zur_registrierung = db.execute(
            "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Zur Registrierung'"
        ).fetchone()[0]
    except Exception:
        ignoriert = 0
        zur_registrierung = 0

    try:
        duplikate_anzahl = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT file_hash FROM idv_files
                WHERE status='active' AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
                GROUP BY file_hash HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
    except Exception:
        duplikate_anzahl = 0

    # ---------- Filter-Optionen ----------
    share_roots = [
        r["share_root"] for r in db.execute("""
            SELECT DISTINCT share_root FROM idv_files
            WHERE share_root IS NOT NULL AND status = 'active'
            ORDER BY share_root
        """).fetchall()
    ]

    dir_paths = [
        r["dir_path"] for r in db.execute(f"""
            SELECT DISTINCT {_DIR_PATH_EXPR_PLAIN} AS dir_path
            FROM idv_files
            WHERE full_path IS NOT NULL AND status = 'active'
            ORDER BY 1
        """).fetchall()
        if r["dir_path"]
    ]
    try:
        scan_runs = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files, archived_files
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 30
        """).fetchall()
    except Exception:
        scan_runs = []

    letzter_scan = scan_runs[0] if scan_runs else None
    is_admin = current_user_role() == ROLE_ADMIN

    persons = db.execute(
        "SELECT id, kuerzel, nachname, vorname FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    gesamt = gesamt_inkl_ignoriert - ignoriert  # Aktive ohne Ignoriert
    return render_template("funde/list.html",
        dateien=dateien, filt=filt,
        total=total, total_pages=total_pages, page=page, per_page=per_page,
        gesamt=gesamt, gesamt_inkl_ignoriert=gesamt_inkl_ignoriert,
        ohne_idv=ohne_idv, mit_makro=mit_makro,
        mit_schutz=mit_schutz, archiviert=archiviert,
        ignoriert=ignoriert, zur_registrierung=zur_registrierung,
        duplikate_anzahl=duplikate_anzahl,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        share_roots=share_roots,
        share_root_filt=share_root,
        dir_paths=dir_paths,
        dir_path_filt=dir_path_filt,
        scan_runs=scan_runs,
        scan_run_id_filt=scan_run_id,
        letzter_scan=letzter_scan,
        scan_run_label=_scan_run_label,
        duplicate_hashes=duplicate_hashes,
        is_admin=is_admin,
        persons=persons,
        sort=sort, order=order,
        q_search=q_search,
        webapp_db_path=current_app.config['DATABASE'],
        valid_per_page=_VALID_PER_PAGE,
        **_scan_btn_ctx(),
    )


@bp.route("/eingang")
@login_required
def eingang_funde():
    """Eingang: Neue, unbearbeitete Scanner-Funde als priorisierte Arbeitsliste."""
    db = get_db()
    dir_path_filt = request.args.get("dir_path", "").strip()
    share_root    = request.args.get("share_root", "").strip()
    scan_run_id   = request.args.get("scan_run", "").strip()
    q_search      = request.args.get("q", "").strip()
    sort          = request.args.get("sort", "prioritaet")
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE:
            session["pref_per_page_funde_eingang"] = per_page
    else:
        per_page = session.get("pref_per_page_funde_eingang", 100)
    if per_page not in _VALID_PER_PAGE:
        per_page = 100
    offset = (page - 1) * per_page

    _no_idv = (
        "NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
        " AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)"
    )
    where_parts = ["f.status = 'active'", "f.bearbeitungsstatus = 'Neu'", _no_idv]
    params = []
    if dir_path_filt:
        where_parts.append(
            f"({_DIR_PATH_EXPR} = ? OR {_DIR_PATH_EXPR} LIKE ? OR {_DIR_PATH_EXPR} LIKE ?)"
        )
        params.extend([dir_path_filt,
                        dir_path_filt + "\\%",
                        dir_path_filt + "/%"])
    if share_root:
        where_parts.append("f.share_root = ?")
        params.append(share_root)
    if scan_run_id:
        try:
            where_parts.append("f.last_scan_run_id = ?")
            params.append(int(scan_run_id))
        except ValueError:
            scan_run_id = ""
    if q_search:
        where_parts.append("f.file_name LIKE ?")
        params.append(f"%{q_search}%")
    where_sql = "WHERE " + " AND ".join(where_parts)

    sort_map = {
        "prioritaet": "f.has_macros DESC, f.formula_count DESC, f.first_seen_at ASC",
        "datum":      "f.first_seen_at DESC",
        "share":      "f.share_root, f.has_macros DESC, f.formula_count DESC",
        "groesse":    "f.size_bytes DESC",
    }
    order_sql = "ORDER BY " + sort_map.get(sort, sort_map["prioritaet"])

    dateien = db.execute(
        f"SELECT f.*, {_DIR_PATH_EXPR} AS dir_path, "
        f"sr.id AS sr_id, sr.started_at AS sr_started_at "
        f"FROM idv_files f LEFT JOIN scan_runs sr ON f.last_scan_run_id = sr.id "
        f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    total = db.execute(
        f"SELECT COUNT(*) FROM idv_files f {where_sql}", params
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Stats-Karten
    neu_gesamt = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Neu'"
    ).fetchone()[0]
    neu_mit_makros = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Neu' AND has_macros=1"
    ).fetchone()[0]
    zur_registrierung_count = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Zur Registrierung'"
    ).fetchone()[0]
    ignoriert_eingang = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Ignoriert'"
    ).fetchone()[0]
    gesamt_aktiv = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active'"
    ).fetchone()[0]
    # Fortschritt: nur nicht-ignorierte Dateien zählen
    gesamt_zu_bearbeiten = gesamt_aktiv - ignoriert_eingang

    # Hotspot-Tabellen
    nach_share = db.execute(f"""
        SELECT {_DIR_PATH_EXPR_PLAIN} AS dir_path,
               COUNT(*) AS anzahl,
               SUM(has_macros) AS mit_makros,
               SUM(CASE WHEN formula_count > 0 THEN 1 ELSE 0 END) AS mit_formeln
        FROM idv_files
        WHERE status='active' AND bearbeitungsstatus='Neu'
        GROUP BY 1
        ORDER BY anzahl DESC
        LIMIT 10
    """).fetchall()

    nach_typ = db.execute("""
        SELECT extension,
               COUNT(*) AS anzahl,
               SUM(has_macros) AS mit_makros
        FROM idv_files
        WHERE status='active' AND bearbeitungsstatus='Neu'
        GROUP BY extension
        ORDER BY anzahl DESC
        LIMIT 8
    """).fetchall()

    dir_paths = [
        r["dir_path"] for r in db.execute(f"""
            SELECT DISTINCT {_DIR_PATH_EXPR_PLAIN} AS dir_path
            FROM idv_files
            WHERE full_path IS NOT NULL AND status='active' AND bearbeitungsstatus='Neu'
            ORDER BY 1
        """).fetchall()
        if r["dir_path"]
    ]

    share_roots = [
        r["share_root"] for r in db.execute("""
            SELECT DISTINCT share_root FROM idv_files
            WHERE share_root IS NOT NULL AND status = 'active' AND bearbeitungsstatus = 'Neu'
            ORDER BY share_root
        """).fetchall()
    ]

    try:
        scan_runs = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files, archived_files
            FROM scan_runs ORDER BY started_at DESC LIMIT 30
        """).fetchall()
    except Exception:
        scan_runs = []

    # Duplikate datenbankweit ermitteln (nicht nur auf der aktuellen Seite)
    duplicate_hashes = {
        r["file_hash"] for r in db.execute("""
            SELECT file_hash FROM idv_files
            WHERE status = 'active'
              AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        """).fetchall()
    }
    is_admin = current_user_role() == ROLE_ADMIN

    persons = db.execute(
        "SELECT id, kuerzel, nachname, vorname FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    return render_template("funde/eingang.html",
        dateien=dateien,
        total=total, total_pages=total_pages,
        page=page, per_page=per_page,
        neu_gesamt=neu_gesamt,
        neu_mit_makros=neu_mit_makros,
        zur_registrierung_count=zur_registrierung_count,
        ignoriert_eingang=ignoriert_eingang,
        gesamt_aktiv=gesamt_aktiv,
        gesamt_zu_bearbeiten=gesamt_zu_bearbeiten,
        nach_share=nach_share,
        nach_typ=nach_typ,
        dir_paths=dir_paths,
        dir_path_filt=dir_path_filt,
        share_roots=share_roots,
        share_root_filt=share_root,
        scan_runs=scan_runs,
        scan_run_id_filt=scan_run_id,
        scan_run_label=_scan_run_label,
        sort=sort,
        q_search=q_search,
        duplicate_hashes=duplicate_hashes,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        is_admin=is_admin,
        persons=persons,
        valid_per_page=_VALID_PER_PAGE,
        **_scan_btn_ctx(),
    )


@bp.route("/bewertet")
@login_required
def bewertet():
    """Redirect zur Ignoriert-Seite (Abwärtskompatibilität)."""
    return redirect(url_for("funde.ignorierte_dateien"))


@bp.route("/ignoriert")
@login_required
def ignorierte_dateien():
    """Eigene Seite: Ignorierte Scanner-Funde."""
    db = get_db()
    dir_path_filt = request.args.get("dir_path", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 100
        if per_page in _VALID_PER_PAGE:
            session["pref_per_page_funde_ignoriert"] = per_page
    else:
        per_page = session.get("pref_per_page_funde_ignoriert", 100)
    if per_page not in _VALID_PER_PAGE:
        per_page = 100

    where_parts = ["f.status = 'active'", "f.bearbeitungsstatus = 'Ignoriert'"]
    params: list = []
    if dir_path_filt:
        where_parts.append(
            f"({_DIR_PATH_EXPR} = ? OR {_DIR_PATH_EXPR} LIKE ? OR {_DIR_PATH_EXPR} LIKE ?)"
        )
        params.extend([dir_path_filt,
                        dir_path_filt + "\\%",
                        dir_path_filt + "/%"])
    where_sql = "WHERE " + " AND ".join(where_parts)

    ignoriert_count = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Ignoriert'"
    ).fetchone()[0]

    total = db.execute(
        f"SELECT COUNT(*) FROM idv_files f {where_sql}", params
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    ignorierte = db.execute(f"""
        SELECT f.*,
               {_DIR_PATH_EXPR} AS dir_path,
               reg.idv_id      AS reg_idv_id,
               reg.bezeichnung AS reg_bezeichnung,
               reg.id          AS reg_db_id,
               sr.id           AS sr_id,
               sr.started_at   AS sr_started_at
        FROM idv_files f
        LEFT JOIN idv_register reg ON reg.file_id = f.id
        LEFT JOIN scan_runs    sr  ON f.last_scan_run_id = sr.id
        {where_sql}
        ORDER BY f.last_seen_at DESC, f.modified_at DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()

    dir_paths = [
        r["dir_path"] for r in db.execute(f"""
            SELECT DISTINCT {_DIR_PATH_EXPR_PLAIN} AS dir_path
            FROM idv_files
            WHERE full_path IS NOT NULL AND status = 'active' AND bearbeitungsstatus = 'Ignoriert'
            ORDER BY 1
        """).fetchall()
        if r["dir_path"]
    ]

    return render_template("funde/ignoriert.html",
        ignorierte=ignorierte,
        ignoriert_count=ignoriert_count,
        total=total, total_pages=total_pages, page=page, per_page=per_page,
        dir_paths=dir_paths, dir_path_filt=dir_path_filt,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        valid_per_page=_VALID_PER_PAGE,
        **_scan_btn_ctx(),
    )


@bp.route("/ignoriert/reaktivieren", methods=["POST"])
@write_access_required
def ignorierte_reaktivieren():
    """Setzt ausgewählte ignorierte Dateien auf 'Neu' zurück (Bulk-Reaktivierung)."""
    db = get_db()
    raw_ids = request.form.getlist("file_ids")
    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige Datei-IDs.", "error")
        return redirect(url_for("funde.ignorierte_dateien"))

    if not file_ids:
        flash("Keine Einträge ausgewählt.", "warning")
        return redirect(url_for("funde.ignorierte_dateien"))

    ph, ph_params = in_clause(file_ids)
    db.execute(
        f"UPDATE idv_files SET bearbeitungsstatus='Neu'"
        f" WHERE id IN ({ph}) AND bearbeitungsstatus='Ignoriert'",
        ph_params,
    )
    db.commit()
    flash(f"{len(file_ids)} Datei(en) reaktiviert.", "success")
    return redirect(url_for("funde.ignorierte_dateien"))


@bp.route("/ignoriert/loeschen", methods=["POST"])
@admin_required
def ignorierte_loeschen():
    """Löscht ausgewählte ignorierte Scanner-Funde dauerhaft (nur Admins)."""
    db = get_db()
    raw_ids = request.form.getlist("file_ids")
    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige Datei-IDs.", "error")
        return redirect(url_for("funde.ignorierte_dateien"))

    if not file_ids:
        flash("Keine Einträge ausgewählt.", "warning")
        return redirect(url_for("funde.ignorierte_dateien"))

    geloescht    = 0
    uebersprungen = 0
    for file_id in file_ids:
        # Nur wirklich ignorierte Dateien betreffen
        datei = db.execute(
            "SELECT id, file_name FROM idv_files WHERE id=? AND bearbeitungsstatus='Ignoriert'",
            (file_id,)
        ).fetchone()
        if not datei:
            continue
        # Keine verknüpften IDVs löschen
        if db.execute("SELECT id FROM idv_register WHERE file_id=?", (file_id,)).fetchone():
            uebersprungen += 1
            continue
        db.execute("DELETE FROM idv_file_history WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM idv_file_links  WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM idv_files WHERE id=?", (file_id,))
        geloescht += 1

    db.commit()
    if geloescht:
        flash(f"{geloescht} Einträge gelöscht.", "success")
    if uebersprungen:
        flash(f"{uebersprungen} Einträge übersprungen (mit IDV verknüpft).", "warning")
    if not geloescht and not uebersprungen:
        flash("Keine passenden Einträge gefunden.", "warning")

    return redirect(url_for("funde.ignorierte_dateien"))


@bp.route("/laeufe")
@login_required
def scan_laeufe():
    """Übersicht aller Scan-Läufe."""
    db = get_db()
    try:
        laeufe = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files, moved_files,
                   restored_files, archived_files, errors
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 100
        """).fetchall()
    except Exception:
        laeufe = []
    return render_template("funde/laeufe.html", laeufe=laeufe,
                           scan_run_label=_scan_run_label,
                           **_scan_btn_ctx())


@bp.route("/zusammenfassen", methods=["GET", "POST"])
@own_write_required
def zusammenfassen():
    """Mehrere Scanner-Funde zu einem IDV-Projekt zusammenfassen.

    GET  – Bestätigungsseite mit Dateiliste + Optionen
    POST – Dateien mit bestehendem IDV verknüpfen
           oder Weiterleitung zur IDV-Neuanlage
    """
    db = get_db()

    if request.method == "POST":
        aktion   = request.form.get("aktion", "")
        raw_ids  = request.form.getlist("file_ids")
        try:
            file_ids = [int(i) for i in raw_ids if i]
        except ValueError:
            flash("Ungültige Datei-IDs.", "error")
            return redirect(url_for("funde.list_funde"))

        if not file_ids:
            flash("Keine Dateien ausgewählt.", "warning")
            return redirect(url_for("funde.list_funde"))

        if aktion == "neues_idv":
            # Primärdatei + zusätzliche IDs an IDV-Neuanlage übergeben
            primary_id  = request.form.get("primary_file_id", "")
            extra_ids   = [str(i) for i in file_ids if str(i) != primary_id]
            url = url_for("idv.new_idv",
                          file_id=primary_id,
                          extra_file_ids=",".join(extra_ids))
            return redirect(url)

        elif aktion == "zu_idv":
            idv_db_id = request.form.get("idv_db_id", "")
            try:
                idv_db_id = int(idv_db_id)
            except (ValueError, TypeError):
                flash("Ungültige IDV-Auswahl.", "error")
                return redirect(url_for("funde.list_funde"))

            idv_row = db.execute(
                "SELECT id, idv_id FROM idv_register WHERE id=?", (idv_db_id,)
            ).fetchone()
            if not idv_row:
                flash("IDV nicht gefunden.", "error")
                return redirect(url_for("funde.list_funde"))

            linked = 0
            for fid in file_ids:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                        (idv_db_id, fid)
                    )
                    db.execute(
                        "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id=?",
                        (fid,)
                    )
                    linked += 1
                except Exception:
                    pass
            db.commit()
            flash(
                f"{linked} Datei(en) mit IDV {idv_row['idv_id']} verknüpft.",
                "success"
            )
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

        flash("Unbekannte Aktion.", "error")
        return redirect(url_for("funde.list_funde"))

    # ---------- GET ----------
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
        f"SELECT * FROM idv_files WHERE id IN ({ph}) ORDER BY last_seen_at DESC",
        ph_params
    ).fetchall()

    # Bestehende IDVs für Dropdown
    idvs = db.execute("""
        SELECT id, idv_id, bezeichnung FROM idv_register
        WHERE status NOT IN ('Außer Betrieb', 'Abgelöst')
        ORDER BY idv_id
    """).fetchall()

    return render_template("funde/zusammenfassen.html",
        dateien=dateien,
        idvs=idvs,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        **_scan_btn_ctx(),
    )


@bp.route("/bulk-aktion", methods=["POST"])
@own_write_required
def bulk_aktion():
    """Massenmarkierung von Scanner-Funden (ignorieren / zur Registrierung)."""
    db      = get_db()
    aktion  = request.form.get("aktion", "")
    raw_ids = request.form.getlist("file_ids")

    if aktion == "zusammenfassen":
        # Weiterleitung zur Zusammenfassen-Seite (GET)
        from flask import url_for as _uf
        ids_qs = "&".join(f"file_ids={i}" for i in raw_ids if i)
        return redirect(url_for("funde.zusammenfassen") + "?" + ids_qs)

    if aktion not in ("ignorieren", "nicht_mehr_ignorieren", "zur_registrierung", "nicht_wesentlich", "owner_aendern", "bewertung_anfordern", "loeschen"):
        flash("Ungültige Aktion.", "error")
        return redirect(url_for("funde.list_funde"))

    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige Datei-IDs.", "error")
        return redirect(url_for("funde.list_funde"))

    if not file_ids:
        flash("Keine Dateien ausgewählt.", "warning")
        return redirect(url_for("funde.list_funde"))

    if aktion == "ignorieren":
        from flask import session as _session
        from . import ROLE_ADMIN
        ist_admin = (_session.get("user_role") == ROLE_ADMIN)

        ph, ph_params = in_clause(file_ids)
        if ist_admin:
            # Admins dürfen alle Dateien ignorieren – keine Einschränkungen
            erlaubte_ids = [r["id"] for r in db.execute(
                f"SELECT id FROM idv_files WHERE id IN ({ph})", ph_params
            ).fetchall()]
            abgelehnt = 0
        else:
            # Nur Dateien ohne Formeln und ohne IDV-Verknüpfung
            kandidaten = db.execute(f"""
                SELECT f.id FROM idv_files f
                WHERE f.id IN ({ph})
                  AND (f.formula_count IS NULL OR f.formula_count = 0)
                  AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
            """, ph_params).fetchall()
            erlaubte_ids = [r["id"] for r in kandidaten]
            abgelehnt = len(file_ids) - len(erlaubte_ids)

        if erlaubte_ids:
            ph2, ph2_params = in_clause(erlaubte_ids)
            db.execute(
                f"UPDATE idv_files SET bearbeitungsstatus = 'Ignoriert' WHERE id IN ({ph2})",
                ph2_params
            )
            db.commit()
            msg = f"{len(erlaubte_ids)} Datei(en) als 'Ignoriert' markiert."
            if abgelehnt:
                msg += f" {abgelehnt} Datei(en) übersprungen (Formeln vorhanden oder bereits registriert)."
            flash(msg, "success")
        else:
            flash(
                "Keine der ausgewählten Dateien konnte ignoriert werden "
                "(Formeln vorhanden oder bereits im IDV-Register).",
                "warning"
            )

    elif aktion == "nicht_mehr_ignorieren":
        ph, ph_params = in_clause(file_ids)
        db.execute(
            f"UPDATE idv_files SET bearbeitungsstatus = 'Neu'"
            f" WHERE id IN ({ph}) AND bearbeitungsstatus = 'Ignoriert'",
            ph_params
        )
        db.commit()
        flash(f"{len(file_ids)} Datei(en): Ignorierung aufgehoben.", "success")

    elif aktion == "zur_registrierung":
        ph, ph_params = in_clause(file_ids)
        db.execute(
            f"UPDATE idv_files SET bearbeitungsstatus = 'Zur Registrierung' WHERE id IN ({ph})",
            ph_params
        )
        db.commit()
        flash(f"{len(file_ids)} Datei(en) zur Registrierung vorgemerkt.", "success")

    elif aktion == "nicht_wesentlich":
        ph, ph_params = in_clause(file_ids)
        db.execute(
            f"UPDATE idv_files SET bearbeitungsstatus = 'Nicht wesentlich' WHERE id IN ({ph})",
            ph_params
        )
        db.commit()
        flash(f"{len(file_ids)} Datei(en) als 'Nicht wesentlich' eingestuft.", "success")

    elif aktion == "owner_aendern":
        new_owner = request.form.get("new_owner", "").strip()
        if not new_owner:
            flash("Kein Dateieigentümer angegeben.", "warning")
        else:
            ph, ph_params = in_clause(file_ids)
            db.execute(
                f"UPDATE idv_files SET file_owner = ? WHERE id IN ({ph})",
                [new_owner] + ph_params
            )
            db.commit()
            flash(
                f"{len(file_ids)} Datei(en): Dateieigentümer auf \"{new_owner}\" gesetzt.",
                "success"
            )

    elif aktion == "bewertung_anfordern":
        from ..email_service import notify_file_bewertung_batch, get_app_base_url
        ph, ph_params = in_clause(file_ids)
        dateien = db.execute(
            f"SELECT * FROM idv_files WHERE id IN ({ph})", ph_params
        ).fetchall()

        base_url = get_app_base_url(db)

        # Dateien nach Empfänger-E-Mail gruppieren
        grouped: dict[str, list] = {}
        kein_empfaenger = 0
        for datei in dateien:
            owner = datei["file_owner"] or datei["office_author"] or ""
            email = None
            if owner:
                person = db.execute(
                    "SELECT email FROM persons WHERE (user_id = ? OR kuerzel = ? OR ad_name = ?) AND aktiv = 1 AND email IS NOT NULL",
                    (owner, owner, owner)
                ).fetchone()
                if person:
                    email = person["email"]
            if not email:
                kein_empfaenger += 1
                continue
            grouped.setdefault(email, []).append(datei)

        gesendet = 0
        fehler = 0
        n_dateien_gesendet = 0
        for email, dateien_gruppe in grouped.items():
            try:
                ok = notify_file_bewertung_batch(db, dateien_gruppe, email, base_url)
                if ok:
                    gesendet += 1
                    n_dateien_gesendet += len(dateien_gruppe)
                else:
                    fehler += 1
            except Exception:
                fehler += 1

        msg_parts = []
        if gesendet:
            msg_parts.append(f"{n_dateien_gesendet} Datei(en) in {gesendet} E-Mail(s) gesendet")
        if kein_empfaenger:
            msg_parts.append(f"{kein_empfaenger} ohne zugeordnete E-Mail-Adresse")
        if fehler:
            msg_parts.append(f"{fehler} Fehler beim Versand")
        flash(". ".join(msg_parts) + ".", "success" if gesendet and not fehler else "warning")

    elif aktion == "loeschen":
        if current_user_role() != ROLE_ADMIN:
            flash("Löschen ist nur für Administratoren erlaubt.", "error")
        else:
            geloescht = 0
            uebersprungen = 0
            for file_id in file_ids:
                if db.execute("SELECT id FROM idv_register WHERE file_id=?", (file_id,)).fetchone():
                    uebersprungen += 1
                    continue
                db.execute("DELETE FROM idv_file_history WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM idv_file_links  WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM idv_files WHERE id=?", (file_id,))
                geloescht += 1
            db.commit()
            if geloescht:
                flash(f"{geloescht} Fund/Funde dauerhaft gelöscht.", "success")
            if uebersprungen:
                flash(f"{uebersprungen} Fund/Funde übersprungen (mit IDV-Eintrag verknüpft).", "warning")
            if not geloescht and not uebersprungen:
                flash("Keine Einträge gefunden.", "warning")

    return_to = request.form.get("return_to", "")
    if return_to == "eingang":
        return redirect(url_for("funde.eingang_funde",
            dir_path=request.form.get("dir_path_filt", ""),
            page=request.form.get("page", 1),
            per_page=request.form.get("per_page", 100),
            sort=request.form.get("sort", "prioritaet")))
    return redirect(url_for("funde.list_funde", filter=request.form.get("filt", "")))


@bp.route("/<int:file_id>/loeschen", methods=["POST"])
@admin_required
def loeschen(file_id):
    """Löscht einen Scannerfund-Eintrag dauerhaft (nur für Administratoren)."""
    db = get_db()
    datei = db.execute("SELECT * FROM idv_files WHERE id=?", (file_id,)).fetchone()
    if not datei:
        flash("Datei nicht gefunden.", "error")
        return redirect(url_for("funde.list_funde"))

    idv_link = db.execute(
        "SELECT id, idv_id FROM idv_register WHERE file_id=?", (file_id,)
    ).fetchone()
    if idv_link:
        flash(
            f"Datei ist mit IDV {idv_link['idv_id']} verknüpft und kann nicht gelöscht werden. "
            "Bitte zuerst die IDV-Verknüpfung aufheben.",
            "error"
        )
        return redirect(url_for("funde.list_funde"))

    datei_name = datei["file_name"]
    # Abhängige Einträge vor dem Hauptlöschen entfernen (FK-Constraints)
    db.execute("DELETE FROM idv_file_history WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM idv_file_links  WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM idv_files WHERE id=?", (file_id,))
    db.commit()
    flash(f"Scannerfund \"{datei_name}\" wurde gelöscht.", "success")
    return redirect(url_for("funde.list_funde"))


@bp.route("/<int:file_id>/benachrichtigen", methods=["POST"])
@write_access_required
def notify_file(file_id):
    """Sendet manuell eine E-Mail-Benachrichtigung für einen Scannerfund."""
    db   = get_db()
    file = db.execute("SELECT * FROM idv_files WHERE id=?", (file_id,)).fetchone()
    if not file:
        flash("Datei nicht gefunden.", "error")
        return redirect(url_for("funde.list_funde"))

    recipients = [
        r["email"] for r in db.execute("""
            SELECT email FROM persons
            WHERE aktiv=1 AND email IS NOT NULL
              AND rolle IN ('IDV-Koordinator','IDV-Administrator','IDV-Entwickler')
        """).fetchall()
        if r["email"]
    ]

    if not recipients:
        flash("Keine E-Mail-Empfänger konfiguriert.", "warning")
        return redirect(url_for("funde.list_funde"))

    try:
        from ..email_service import notify_new_scanner_file
        ok = notify_new_scanner_file(db, file, recipients)
        if ok:
            flash(f"Benachrichtigung gesendet an: {', '.join(recipients)}", "success")
        else:
            flash("E-Mail konnte nicht gesendet werden – SMTP-Einstellungen prüfen.", "warning")
    except Exception as exc:
        flash(f"Fehler beim E-Mail-Versand: {exc}", "error")

    return redirect(url_for("funde.list_funde"))
