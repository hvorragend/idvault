"""Funde-Blueprint (Scanner-Ergebnisse)"""
import io
import json
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session, jsonify, send_file
from . import login_required, write_access_required, own_write_required, get_db, admin_required, current_user_role, ROLE_ADMIN, can_write
from ..app_settings import get_bool as _get_bool
from ..db_writer import get_writer
from db_write_tx import write_tx
from ..security import in_clause
from ..helpers import _EXT_TO_TYP, _idv_typ_vorschlag
from .. import similarity as _sim

log = logging.getLogger("idvault.funde")

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

# Excel-Dateiformate, für die der Scanner Blatt-/Arbeitsmappenschutz auslesen kann
# (OOXML-Container; .xls bleibt außen vor, weil der Scanner dort keine Protection-
# Flags ermitteln kann und das sonst zu falschen IT-Risiko-Meldungen führen würde).
_EXCEL_PROTECTABLE_EXTS = (".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx")
_EXCEL_EXT_PLACEHOLDERS = ",".join("?" * len(_EXCEL_PROTECTABLE_EXTS))


def _compute_match_scores(dateien, db):
    """For unregistered funds compute the best-matching IDV score.

    Returns {file_id: {"score": int, "idv_db_id": int, "idv_id": str, "bezeichnung": str}}.
    Nur Einträge oberhalb der konfigurierten Schwelle (``similarity_config.threshold``)
    werden aufgenommen. Das Scoring selbst liegt zentral in ``webapp/similarity.py``.
    """
    unregistered = [f for f in dateien if not f["reg_idv_id"]]
    if not unregistered:
        return {}

    idv_candidates = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.idv_typ,
               p_e.user_id  AS dev_uid,
               p_e.ad_name  AS dev_ad,
               p_f.user_id  AS fv_uid,
               p_f.ad_name  AS fv_ad
        FROM idv_register r
        LEFT JOIN persons p_e ON r.idv_entwickler_id      = p_e.id
        LEFT JOIN persons p_f ON r.fachverantwortlicher_id = p_f.id
        WHERE r.status NOT IN ('Außer Betrieb', 'Abgelöst')
    """).fetchall()

    if not idv_candidates:
        return {}

    cfg   = _sim.get_config(db)
    noise = frozenset(cfg["noise_words"])
    threshold = cfg["threshold"]

    result = {}
    for fund in unregistered:
        fund_typ   = _idv_typ_vorschlag(fund["extension"], fund["has_macros"])
        fund_owner = fund["file_owner"] or ""
        fund_name  = fund["file_name"] or ""

        best_score = 0
        best_idv   = None

        for idv in idv_candidates:
            dev_ids = (
                idv["dev_uid"], idv["dev_ad"],
                idv["fv_uid"], idv["fv_ad"],
            )
            score = _sim.score_pair(
                fund_typ=fund_typ,
                fund_owner=fund_owner,
                fund_name=fund_name,
                idv_typ=idv["idv_typ"] or "",
                idv_name=idv["bezeichnung"] or "",
                dev_ids_lower=[d for d in dev_ids if d],
                config=cfg,
                noise=noise,
            )
            if score > best_score:
                best_score = score
                best_idv   = idv

        if best_score >= threshold and best_idv:
            result[fund["id"]] = {
                "score":       best_score,
                "idv_db_id":   best_idv["id"],
                "idv_id":      best_idv["idv_id"],
                "bezeichnung": best_idv["bezeichnung"],
            }

    return result


_FUNDE_SORT_COLS = {
    "dateiname":  "f.file_name",
    "groesse":    "f.size_bytes",
    "geaendert":  "f.modified_at",
    "scan":       "f.last_seen_at",
    "prioritaet": "(f.has_macros * 1000000 + COALESCE(f.formula_count, 0))",
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
    owner_filt  = request.args.get("owner", "").strip()
    date_from   = request.args.get("date_from", "").strip()
    date_to     = request.args.get("date_to", "").strip()
    sort        = request.args.get("sort", "scan").strip()
    order       = request.args.get("order", "desc").strip()
    highlight_raw = request.args.get("highlight", "").strip()
    try:
        highlight_id = int(highlight_raw) if highlight_raw else None
    except ValueError:
        highlight_id = None
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
        elif filt == "ohne_schutz":
            # Excel-Dateien ohne Blatt- und Arbeitsmappenschutz
            # → Kandidaten für IT-Risiko-Erfassung (MaRisk AT 7.2 / DORA)
            where_parts.append(
                f"LOWER(f.extension) IN ({_EXCEL_EXT_PLACEHOLDERS})"
            )
            params.extend(_EXCEL_PROTECTABLE_EXTS)
            where_parts.append("COALESCE(f.has_sheet_protection, 0) = 0")
            where_parts.append("COALESCE(f.workbook_protected, 0) = 0")
            where_parts.append(
                "(f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')"
            )
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

    if owner_filt:
        where_parts.append("f.file_owner = ?")
        params.append(owner_filt)

    if date_from:
        where_parts.append("f.modified_at >= ?")
        params.append(date_from)

    if date_to:
        where_parts.append("f.modified_at <= ?")
        params.append(date_to + "T23:59:59")

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

    # Wenn highlight gesetzt und Datei nicht auf aktueller Seite: zur richtigen Seite weiterleiten
    if highlight_id and not any(f["id"] == highlight_id for f in dateien):
        all_ids = [r["id"] for r in db.execute(
            f"SELECT f.id FROM idv_files f {where_sql} {order_sql}", params
        ).fetchall()]
        if highlight_id in all_ids:
            target_page = all_ids.index(highlight_id) // per_page + 1
            args = request.args.to_dict()
            args["page"] = str(target_page)
            return redirect(url_for("funde.list_funde", **args))

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
    ohne_schutz = db.execute(
        "SELECT COUNT(*) FROM idv_files"
        " WHERE status='active'"
        f"   AND LOWER(extension) IN ({_EXCEL_EXT_PLACEHOLDERS})"
        "   AND COALESCE(has_sheet_protection, 0) = 0"
        "   AND COALESCE(workbook_protected, 0) = 0"
        "   AND (bearbeitungsstatus IS NULL OR bearbeitungsstatus != 'Ignoriert')",
        _EXCEL_PROTECTABLE_EXTS,
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
    owner_list = [
        r["file_owner"] for r in db.execute(
            "SELECT DISTINCT file_owner FROM idv_files"
            " WHERE file_owner IS NOT NULL AND file_owner != '' AND status='active'"
            " ORDER BY file_owner"
        ).fetchall()
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
        "SELECT id, user_id, nachname, vorname FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    gesamt = gesamt_inkl_ignoriert - ignoriert  # Aktive ohne Ignoriert

    # Match-Score-Vorschläge nur berechnen wenn relevante Filter aktiv sind und Funktion aktiv
    if filt not in ("archiv", "duplikate", "mit_idv") and _get_bool(db, "match_suggestions_enabled", True):
        match_scores = _compute_match_scores(dateien, db)
    else:
        match_scores = {}

    # Issue #355: Eskalations-Stufe pro Owner (None / 'reminder' / 'oe_lead' / 'coordinator')
    try:
        from db import get_self_service_escalation_stages
        escalation_stages = get_self_service_escalation_stages(db)
    except Exception:
        escalation_stages = {}

    return render_template("funde/list.html",
        dateien=dateien, filt=filt,
        total=total, total_pages=total_pages, page=page, per_page=per_page,
        gesamt=gesamt, gesamt_inkl_ignoriert=gesamt_inkl_ignoriert,
        ohne_idv=ohne_idv, mit_makro=mit_makro,
        mit_schutz=mit_schutz, ohne_schutz=ohne_schutz, archiviert=archiviert,
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
        highlight_id=highlight_id,
        owner_list=owner_list,
        owner_filt=owner_filt,
        date_from=date_from,
        date_to=date_to,
        webapp_db_path=current_app.config['DATABASE'],
        valid_per_page=_VALID_PER_PAGE,
        match_scores=match_scores,
        escalation_stages=escalation_stages,
        **_scan_btn_ctx(),
    )


@bp.route("/export/ohne-schutz.xlsx")
@login_required
def export_ohne_schutz():
    """Excel-Report aller Excel-Dateien ohne Blatt-/Arbeitsmappenschutz.

    Dient als Arbeitsgrundlage für die Anlage von IT-Risiken im IDV-Register
    (MaRisk AT 7.2 / DORA).
    """
    from ..excel_export import unprotected_excel_bytes
    payload = unprotected_excel_bytes(get_db())
    fname = f"excel-ohne-zellschutz-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    return send_file(
        io.BytesIO(payload),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


@bp.route("/<int:file_id>/quick-assign", methods=["POST"])
@own_write_required
def quick_assign(file_id):
    """1-Klick-Zuordnung eines Scanner-Funds zu einer IDV (AJAX)."""
    try:
        idv_db_id = int(request.form.get("idv_db_id", ""))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Ungültige IDV-ID."}), 400

    db = get_db()
    idv = db.execute(
        "SELECT id, idv_id FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not idv:
        return jsonify({"ok": False, "error": "Eigenentwicklung nicht gefunden."}), 404

    datei = db.execute("SELECT id FROM idv_files WHERE id = ?", (file_id,)).fetchone()
    if not datei:
        return jsonify({"ok": False, "error": "Fund nicht gefunden."}), 404

    def _do(c):
        with write_tx(c):
            c.execute(
                "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                (idv_db_id, file_id),
            )
            c.execute(
                "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id = ?",
                (file_id,),
            )

    get_writer().submit(_do, wait=True)
    return jsonify({
        "ok":        True,
        "idv_id":    idv["idv_id"],
        "idv_db_id": idv_db_id,
        "detail_url": url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id),
    })


@bp.route("/eingang")
@login_required
def eingang_funde():
    """Eingang: Neue, unbearbeitete Scanner-Funde als priorisierte Arbeitsliste."""
    db = get_db()
    dir_path_filt = request.args.get("dir_path", "").strip()
    share_root    = request.args.get("share_root", "").strip()
    scan_run_id   = request.args.get("scan_run", "").strip()
    q_search      = request.args.get("q", "").strip()
    owner_filt    = request.args.get("owner", "").strip()
    date_from     = request.args.get("date_from", "").strip()
    date_to       = request.args.get("date_to", "").strip()
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
    if owner_filt:
        where_parts.append("f.file_owner = ?")
        params.append(owner_filt)
    if date_from:
        where_parts.append("f.modified_at >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("f.modified_at <= ?")
        params.append(date_to + "T23:59:59")
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
        "SELECT id, user_id, nachname, vorname FROM persons WHERE aktiv=1 ORDER BY nachname, vorname"
    ).fetchall()

    owner_list = [
        r["file_owner"] for r in db.execute(
            "SELECT DISTINCT file_owner FROM idv_files"
            " WHERE file_owner IS NOT NULL AND file_owner != ''"
            " AND status='active' AND bearbeitungsstatus='Neu'"
            " ORDER BY file_owner"
        ).fetchall()
    ]

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
        owner_list=owner_list,
        owner_filt=owner_filt,
        date_from=date_from,
        date_to=date_to,
        valid_per_page=_VALID_PER_PAGE,
        **_scan_btn_ctx(),
    )


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
    sql = (
        f"UPDATE idv_files SET bearbeitungsstatus='Neu'"
        f" WHERE id IN ({ph}) AND bearbeitungsstatus='Ignoriert'"
    )

    def _do(c):
        with write_tx(c):
            c.execute(sql, ph_params)

    get_writer().submit(_do, wait=True)
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

    # Pre-Filter auf der Reader-Connection: nur wirklich ignorierte Dateien
    # ohne IDV-Verknuepfung kommen durch.
    uebersprungen = 0
    loeschbare = []
    for file_id in file_ids:
        datei = db.execute(
            "SELECT id FROM idv_files WHERE id=? AND bearbeitungsstatus='Ignoriert'",
            (file_id,),
        ).fetchone()
        if not datei:
            continue
        if db.execute("SELECT id FROM idv_register WHERE file_id=?", (file_id,)).fetchone():
            uebersprungen += 1
            continue
        loeschbare.append(file_id)

    def _do(c):
        count = 0
        with write_tx(c):
            for fid in loeschbare:
                c.execute("DELETE FROM idv_file_history WHERE file_id=?", (fid,))
                c.execute("DELETE FROM idv_file_links  WHERE file_id=?", (fid,))
                c.execute("DELETE FROM idv_files WHERE id=?", (fid,))
                count += 1
        return count

    geloescht = get_writer().submit(_do, wait=True) if loeschbare else 0
    if geloescht:
        flash(f"{geloescht} Einträge gelöscht.", "success")
    if uebersprungen:
        flash(f"{uebersprungen} Einträge übersprungen (mit Eigenentwicklung verknüpft).", "warning")
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
            url = url_for("eigenentwicklung.new_idv",
                          file_id=primary_id,
                          extra_file_ids=",".join(extra_ids))
            return redirect(url)

        elif aktion == "zu_idv":
            idv_db_id = request.form.get("idv_db_id", "")
            try:
                idv_db_id = int(idv_db_id)
            except (ValueError, TypeError):
                flash("Ungültige Auswahl der Eigenentwicklung.", "error")
                return redirect(url_for("funde.list_funde"))

            idv_row = db.execute(
                "SELECT id, idv_id FROM idv_register WHERE id=?", (idv_db_id,)
            ).fetchone()
            if not idv_row:
                flash("Eigenentwicklung nicht gefunden.", "error")
                return redirect(url_for("funde.list_funde"))

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
            flash(
                f"{linked} Datei(en) mit IDV {idv_row['idv_id']} verknüpft.",
                "success"
            )
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

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
        ids_qs = "&".join(f"file_ids={i}" for i in raw_ids if i)
        return redirect(url_for("funde.zusammenfassen") + "?" + ids_qs)

    if aktion == "bulk_registrieren":
        # Weiterleitung zur Bulk-Registrierungs-Seite (je Datei eine eigene IDV)
        ids_qs = "&".join(f"file_ids={i}" for i in raw_ids if i)
        return redirect(url_for("eigenentwicklung.bulk_neu") + "?" + ids_qs)

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
            sql = f"UPDATE idv_files SET bearbeitungsstatus = 'Ignoriert' WHERE id IN ({ph2})"

            def _do(c):
                with write_tx(c):
                    c.execute(sql, ph2_params)

            get_writer().submit(_do, wait=True)
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
        sql = (
            f"UPDATE idv_files SET bearbeitungsstatus = 'Neu'"
            f" WHERE id IN ({ph}) AND bearbeitungsstatus = 'Ignoriert'"
        )

        def _do(c):
            with write_tx(c):
                c.execute(sql, ph_params)

        get_writer().submit(_do, wait=True)
        flash(f"{len(file_ids)} Datei(en): Ignorierung aufgehoben.", "success")

    elif aktion == "zur_registrierung":
        ph, ph_params = in_clause(file_ids)
        sql = f"UPDATE idv_files SET bearbeitungsstatus = 'Zur Registrierung' WHERE id IN ({ph})"

        def _do(c):
            with write_tx(c):
                c.execute(sql, ph_params)

        get_writer().submit(_do, wait=True)
        flash(f"{len(file_ids)} Datei(en) zur Registrierung vorgemerkt.", "success")

    elif aktion == "nicht_wesentlich":
        ph, ph_params = in_clause(file_ids)
        sql = f"UPDATE idv_files SET bearbeitungsstatus = 'Nicht wesentlich' WHERE id IN ({ph})"

        def _do(c):
            with write_tx(c):
                c.execute(sql, ph_params)

        get_writer().submit(_do, wait=True)
        flash(f"{len(file_ids)} Datei(en) als 'Nicht wesentlich' eingestuft.", "success")

    elif aktion == "owner_aendern":
        new_owner = request.form.get("new_owner", "").strip()
        if not new_owner:
            flash("Kein Dateieigentümer angegeben.", "warning")
        else:
            ph, ph_params = in_clause(file_ids)
            sql = f"UPDATE idv_files SET file_owner = ? WHERE id IN ({ph})"
            params = [new_owner] + ph_params

            def _do(c):
                with write_tx(c):
                    c.execute(sql, params)

            get_writer().submit(_do, wait=True)
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
                    "SELECT email FROM persons WHERE (user_id = ? OR ad_name = ?) AND aktiv = 1 AND email IS NOT NULL",
                    (owner, owner)
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
            except Exception as exc:
                log.exception("Fehler beim Versand an %s: %s", email, exc)
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
        from . import ROLE_ADMIN as _ROLE_ADMIN
        if current_user_role() != _ROLE_ADMIN:
            flash("Löschen ist nur für Administratoren erlaubt.", "error")
        else:
            uebersprungen = 0
            loeschbare = []
            for file_id in file_ids:
                if db.execute("SELECT id FROM idv_register WHERE file_id=?", (file_id,)).fetchone():
                    uebersprungen += 1
                    continue
                loeschbare.append(file_id)

            def _do(c):
                count = 0
                with write_tx(c):
                    for fid in loeschbare:
                        c.execute("DELETE FROM idv_file_history WHERE file_id=?", (fid,))
                        c.execute("DELETE FROM idv_file_links  WHERE file_id=?", (fid,))
                        c.execute("DELETE FROM idv_files WHERE id=?", (fid,))
                        count += 1
                return count

            geloescht = get_writer().submit(_do, wait=True) if loeschbare else 0
            if geloescht:
                flash(f"{geloescht} Fund/Funde dauerhaft gelöscht.", "success")
            if uebersprungen:
                flash(f"{uebersprungen} Fund/Funde übersprungen (mit Eigenentwicklung verknüpft).", "warning")
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

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM idv_file_history WHERE file_id=?", (file_id,))
            c.execute("DELETE FROM idv_file_links  WHERE file_id=?", (file_id,))
            c.execute("DELETE FROM idv_files WHERE id=?", (file_id,))

    get_writer().submit(_do, wait=True)
    flash(f"Scannerfund \"{datei_name}\" wurde gelöscht.", "success")
    return redirect(url_for("funde.list_funde"))


@bp.route("/auto-zuordnen", methods=["POST"])
@write_access_required
def auto_zuordnen():
    """Batch: ordnet neue Funde mit sehr hohem Score automatisch ihrem
    besten IDV-Kandidaten zu.

    Die Zuordnung greift nur, wenn:
      - Score ≥ ``similarity_config.auto_assign_threshold``
      - Plausibilität erfüllt (Typ-Match oder Owner-Match gegen Entwickler/FV)
      - Fund bislang ``Neu`` und noch keinem IDV zugeordnet

    Jede Zuordnung erhält einen History-Eintrag ``scan_auto_assigned`` und
    lässt sich über die normale Fund-Detailansicht wieder lösen.
    """
    db = get_db()
    person_id = session.get("person_id")
    user_name = session.get("user_name", "") or None

    cfg   = _sim.get_config(db)
    noise = frozenset(cfg["noise_words"])
    auto_threshold    = cfg["auto_assign_threshold"]
    suggest_threshold = cfg.get("suggest_threshold", 0) or 0
    hash_dedup        = bool(cfg.get("auto_link_hash_duplicates", True))
    version_link      = bool(cfg.get("auto_link_version_series", True))

    neu_funde = db.execute(f"""
        SELECT f.id, f.file_name, f.extension, f.file_owner, f.has_macros,
               f.file_hash, f.version_fingerprint
          FROM idv_files f
         WHERE f.status='active' AND f.bearbeitungsstatus='Neu'
           AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
           AND NOT EXISTS (SELECT 1 FROM idv_file_links l WHERE l.file_id = f.id)
    """).fetchall()

    if not neu_funde:
        flash("Keine neuen Funde für die Auto-Zuordnung gefunden.", "info")
        return redirect(url_for("funde.eingang_funde"))

    # ── Schritt 1: Hash-Dubletten automatisch als Zusatz-Link ──
    hash_plan = []  # list of dicts {file_id, file_name, idv_db_id, idv_id}
    used_file_ids: set[int] = set()
    if hash_dedup:
        for fund in neu_funde:
            h = (fund["file_hash"] or "").strip()
            if not h or h == "HASH_ERROR":
                continue
            # Ziel-IDV(s) ermitteln: IDVs mit derselben Hauptdatei oder Zusatz-Link
            targets = db.execute(
                """
                SELECT DISTINCT r.id AS idv_db_id, r.idv_id
                  FROM idv_register r
                  JOIN idv_files f ON r.file_id = f.id
                 WHERE f.file_hash = ? AND f.id != ?
                UNION
                SELECT DISTINCT r.id AS idv_db_id, r.idv_id
                  FROM idv_register r
                  JOIN idv_file_links l ON l.idv_db_id = r.id
                  JOIN idv_files f      ON f.id        = l.file_id
                 WHERE f.file_hash = ? AND f.id != ?
                """,
                (h, fund["id"], h, fund["id"]),
            ).fetchall()
            if len(targets) == 1:
                t = targets[0]
                hash_plan.append({
                    "file_id":   fund["id"],
                    "file_name": fund["file_name"] or "",
                    "idv_db_id": t["idv_db_id"],
                    "idv_id":    t["idv_id"],
                    "hash":      h,
                })
                used_file_ids.add(fund["id"])

    # ── Schritt 1b: Versions-Serien-Fingerprint als Zusatz-Link (#359) ──
    # Fund hat denselben ``version_fingerprint`` wie genau eine bereits
    # verlinkte Datei → automatisch verknuepfen. Mehrere Treffer in
    # verschiedenen IDVs landen weiter im normalen Similarity-Pfad und
    # werden ggf. als Vorschlag angeboten.
    version_plan: list = []
    if version_link:
        for fund in neu_funde:
            if fund["id"] in used_file_ids:
                continue
            fp = (fund["version_fingerprint"] or "").strip()
            if not fp:
                continue
            targets = db.execute(
                """
                SELECT DISTINCT r.id AS idv_db_id, r.idv_id
                  FROM idv_register r
                  JOIN idv_files f ON r.file_id = f.id
                 WHERE f.version_fingerprint = ? AND f.id != ?
                UNION
                SELECT DISTINCT r.id AS idv_db_id, r.idv_id
                  FROM idv_register r
                  JOIN idv_file_links l ON l.idv_db_id = r.id
                  JOIN idv_files f      ON f.id        = l.file_id
                 WHERE f.version_fingerprint = ? AND f.id != ?
                """,
                (fp, fund["id"], fp, fund["id"]),
            ).fetchall()
            if len(targets) == 1:
                t = targets[0]
                version_plan.append({
                    "file_id":     fund["id"],
                    "file_name":   fund["file_name"] or "",
                    "idv_db_id":   t["idv_db_id"],
                    "idv_id":      t["idv_id"],
                    "fingerprint": fp,
                })
                used_file_ids.add(fund["id"])

    remaining = [f for f in neu_funde if f["id"] not in used_file_ids]

    idv_candidates = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.idv_typ,
               p_e.user_id AS dev_uid, p_e.ad_name AS dev_ad,
               p_f.user_id AS fv_uid,  p_f.ad_name AS fv_ad
          FROM idv_register r
          LEFT JOIN persons p_e ON r.idv_entwickler_id      = p_e.id
          LEFT JOIN persons p_f ON r.fachverantwortlicher_id = p_f.id
         WHERE r.status NOT IN ('Außer Betrieb', 'Abgelöst')
    """).fetchall()
    if not idv_candidates:
        flash("Keine Eigenentwicklungen für Auto-Zuordnung verfügbar.", "info")
        return redirect(url_for("funde.eingang_funde"))

    plan = []
    suggest_plan = []  # Mid-Score-Treffer für die Vorschlagsliste
    for fund in remaining:
        fund_typ   = _idv_typ_vorschlag(fund["extension"], fund["has_macros"])
        fund_owner = fund["file_owner"] or ""
        fund_name  = fund["file_name"] or ""
        best_score, best_idv = 0, None
        for idv in idv_candidates:
            dev_ids = [
                idv["dev_uid"], idv["dev_ad"],
                idv["fv_uid"], idv["fv_ad"],
            ]
            score = _sim.score_pair(
                fund_typ=fund_typ, fund_owner=fund_owner, fund_name=fund_name,
                idv_typ=idv["idv_typ"] or "", idv_name=idv["bezeichnung"] or "",
                dev_ids_lower=[d for d in dev_ids if d],
                config=cfg, noise=noise,
            )
            if score > best_score:
                best_score, best_idv = score, idv
        if not best_idv:
            continue
        dev_ids = [
            best_idv["dev_uid"], best_idv["dev_ad"],
            best_idv["fv_uid"], best_idv["fv_ad"],
        ]
        plausible = _sim.is_plausible_auto_match(
            fund_typ=fund_typ, fund_owner=fund_owner,
            idv_typ=best_idv["idv_typ"] or "",
            dev_ids_lower=[d for d in dev_ids if d],
        )
        if best_score >= auto_threshold and plausible:
            plan.append({
                "file_id":   fund["id"],
                "file_name": fund_name,
                "idv_db_id": best_idv["id"],
                "idv_id":    best_idv["idv_id"],
                "score":     best_score,
            })
        elif (
            suggest_threshold > 0
            and best_score >= suggest_threshold
            and best_score < auto_threshold
            and plausible
        ):
            suggest_plan.append({
                "file_id":   fund["id"],
                "file_name": fund_name,
                "idv_db_id": best_idv["id"],
                "idv_id":    best_idv["idv_id"],
                "score":     best_score,
            })

    if not plan and not hash_plan and not suggest_plan and not version_plan:
        flash(
            f"Keine Hash-Dubletten oder Versions-Serien gefunden und keine "
            f"Funde erreichten die Auto-Schwelle von {auto_threshold} mit "
            f"erfüllter Plausibilität.",
            "info",
        )
        return redirect(url_for("funde.eingang_funde"))

    def _do(c):
        with write_tx(c):
            for entry in hash_plan:
                c.execute(
                    "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                    (entry["idv_db_id"], entry["file_id"]),
                )
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id = ?",
                    (entry["file_id"],),
                )
                c.execute(
                    "INSERT INTO idv_history "
                    "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                    "VALUES (?,?,?,?,?)",
                    (entry["idv_db_id"], "scan_auto_linked_hash",
                     f"Hash-Dublette '{entry['file_name']}' (file_id={entry['file_id']}) "
                     f"automatisch verknüpft (SHA-256 identisch: {entry['hash']})",
                     person_id, user_name),
                )
            for entry in version_plan:
                c.execute(
                    "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                    (entry["idv_db_id"], entry["file_id"]),
                )
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id = ?",
                    (entry["file_id"],),
                )
                c.execute(
                    "INSERT INTO idv_history "
                    "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                    "VALUES (?,?,?,?,?)",
                    (entry["idv_db_id"], "scan_auto_linked_version",
                     f"Versions-Serie '{entry['file_name']}' (file_id={entry['file_id']}) "
                     f"automatisch verknüpft (Fingerprint: {entry['fingerprint']})",
                     person_id, user_name),
                )
            for entry in plan:
                c.execute(
                    "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                    (entry["idv_db_id"], entry["file_id"]),
                )
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id = ?",
                    (entry["file_id"],),
                )
                c.execute(
                    "INSERT INTO idv_history "
                    "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                    "VALUES (?,?,?,?,?)",
                    (entry["idv_db_id"], "scan_auto_assigned",
                     f"Scanner-Fund '{entry['file_name']}' (file_id={entry['file_id']}) "
                     f"automatisch zugeordnet (Score {entry['score']} ≥ {auto_threshold})",
                     person_id, user_name),
                )
            for entry in suggest_plan:
                # Vorschlag idempotent anlegen. Bereits entschiedene Vorschläge
                # (decision IS NOT NULL) werden nicht überschrieben — der UNIQUE-
                # Index auf (file_id, idv_db_id) verhindert Duplikate.
                c.execute(
                    "INSERT OR IGNORE INTO idv_match_suggestions "
                    "(file_id, idv_db_id, score) VALUES (?,?,?)",
                    (entry["file_id"], entry["idv_db_id"], entry["score"]),
                )

    get_writer().submit(_do, wait=True)
    parts = []
    if hash_plan:
        parts.append(f"{len(hash_plan)} Hash-Dublette(n) verknüpft")
    if version_plan:
        parts.append(f"{len(version_plan)} Versions-Serien-Treffer verknüpft")
    if plan:
        parts.append(f"{len(plan)} Ähnlichkeits-Treffer(n) ab Schwelle {auto_threshold}")
    if suggest_plan:
        parts.append(
            f"{len(suggest_plan)} Vorschlag/Vorschläge an den Fachbereich "
            f"(Score ≥ {suggest_threshold})"
        )
    flash("Auto-Zuordnung abgeschlossen: " + " · ".join(parts) + ".", "success")
    return redirect(url_for("funde.eingang_funde"))


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
