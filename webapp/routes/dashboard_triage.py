"""Triage-Dashboard fuer den IDV-Koordinator (Issue #353).

Zeigt ausschliesslich Items, die menschliche Triage brauchen — alles
andere ist Aufgabe der Automatisierung. Sechs Kategorien mit
Pagination und Direktaktionen.

Sichtbar fuer ROLE_ADMIN und ROLE_KOORDINATOR. Per Helper
``triage_count(db)`` zur Anzeige der Header-Badge.
"""
from __future__ import annotations

import math
from urllib.parse import urlencode

from flask import Blueprint, render_template, redirect, request, url_for, flash, session
from functools import wraps

from . import login_required, get_db, ROLE_ADMIN, ROLE_KOORDINATOR
from ..db_writer import get_writer

from db_write_tx import write_tx


_DEFAULT_PAGE_SIZE = 25
_PAGE_SIZE_OPTIONS = (10, 25, 50, 100, 200)

_SORTS_MITTLERE_KONFIDENZ = {
    "score_desc": "ORDER BY s.score DESC, s.created_at",
    "score_asc":  "ORDER BY s.score ASC,  s.created_at",
}
_DEFAULT_SORT_MITTLERE_KONFIDENZ = "score_desc"


def _resolve_page_size() -> int:
    """Liest ``?per_page=N`` aus der Query und validiert gegen die
    Whitelist; faellt sonst auf den Default zurueck."""
    raw = request.args.get("per_page", _DEFAULT_PAGE_SIZE)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_PAGE_SIZE
    return n if n in _PAGE_SIZE_OPTIONS else _DEFAULT_PAGE_SIZE


bp = Blueprint("dashboard_triage", __name__, url_prefix="/dashboard")


def _koordinator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        if session.get("user_role") not in (ROLE_ADMIN, ROLE_KOORDINATOR):
            flash("Die Triage-Ansicht ist nur für den IDV-Koordinator.",
                  "error")
            return redirect(url_for("dashboard.index"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Kategorie-Queries
# ---------------------------------------------------------------------------

def _mittlere_konfidenz(db, page=1, page_size=_DEFAULT_PAGE_SIZE,
                        sort=_DEFAULT_SORT_MITTLERE_KONFIDENZ):
    """Auto-Match-Vorschlaege ohne Entscheidung (mittlere Konfidenz)."""
    offset = (page - 1) * page_size
    order_by = _SORTS_MITTLERE_KONFIDENZ.get(
        sort, _SORTS_MITTLERE_KONFIDENZ[_DEFAULT_SORT_MITTLERE_KONFIDENZ]
    )
    try:
        return db.execute(f"""
            SELECT s.id AS suggestion_id, s.score, s.created_at,
                   f.id AS file_id, f.file_name, f.file_owner,
                   r.id AS idv_db_id, r.idv_id, r.bezeichnung
              FROM idv_match_suggestions s
              JOIN idv_files    f ON f.id = s.file_id
              JOIN idv_register r ON r.id = s.idv_db_id
             WHERE s.decision IS NULL
               AND f.status = 'active'
               AND f.bearbeitungsstatus = 'Neu'
               AND r.status NOT IN ('Archiviert')
               AND NOT EXISTS (
                   SELECT 1 FROM triage_verworfen v
                    WHERE v.kategorie = 'mittlere_konfidenz'
                      AND v.ref_key = CAST(s.id AS TEXT)
               )
             {order_by}
             LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    except Exception:
        return []


def _owner_mapping_fehlt(db, page=1, page_size=_DEFAULT_PAGE_SIZE):
    """Scanner-Dateien mit ``file_owner``, der nicht in ``persons`` aufloesbar ist."""
    offset = (page - 1) * page_size
    return db.execute("""
        SELECT f.id AS file_id, f.file_name, f.full_path, f.file_owner,
               f.modified_at
          FROM idv_files f
         WHERE f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND f.file_owner IS NOT NULL
           AND TRIM(f.file_owner) != ''
           AND NOT EXISTS (
               SELECT 1 FROM persons p
                WHERE p.aktiv = 1
                  AND (p.user_id = f.file_owner OR p.ad_name = f.file_owner)
           )
           AND NOT EXISTS (
               SELECT 1 FROM triage_verworfen v
                WHERE v.kategorie = 'owner_fehlt'
                  AND v.ref_key = CAST(f.id AS TEXT)
           )
         ORDER BY f.modified_at DESC
         LIMIT ? OFFSET ?
    """, (page_size, offset)).fetchall()


def _self_service_stumm(db, page=1, page_size=_DEFAULT_PAGE_SIZE):
    """Tokens versendet, > 14 Tage ohne Aktion (kein audit-Eintrag fuer den Token)."""
    offset = (page - 1) * page_size
    try:
        return db.execute("""
            SELECT t.jti, t.person_id, t.created_at, t.expires_at,
                   p.vorname, p.nachname, p.email
              FROM self_service_tokens t
              JOIN persons p ON p.id = t.person_id
             WHERE t.created_at <= datetime('now','-14 days')
               AND t.first_used_at IS NULL
               AND t.revoked_at    IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM self_service_audit a
                    WHERE a.person_id = t.person_id
                      AND a.created_at >= t.created_at
               )
               AND NOT EXISTS (
                   SELECT 1 FROM triage_verworfen v
                    WHERE v.kategorie = 'self_service_stumm'
                      AND v.ref_key = t.jti
               )
             ORDER BY t.created_at
             LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    except Exception:
        return []


def _auto_classify_fehlgeschlagen(db, page=1, page_size=_DEFAULT_PAGE_SIZE):
    """IDV-Files mit Treffer auf eine Auto-Klassifizier-Regel, aber unklassifiziert."""
    offset = (page - 1) * page_size
    try:
        return db.execute("""
            SELECT f.id AS file_id, f.file_name, f.full_path,
                   f.bearbeitungsstatus, f.modified_at,
                   (SELECT COUNT(*) FROM auto_classify_rules WHERE aktiv=1) AS regel_anzahl
              FROM idv_files f
             WHERE f.status = 'active'
               AND f.bearbeitungsstatus = 'Auto-Klassifizierung fehlgeschlagen'
               AND NOT EXISTS (
                   SELECT 1 FROM triage_verworfen v
                    WHERE v.kategorie = 'auto_classify_failed'
                      AND v.ref_key = CAST(f.id AS TEXT)
               )
             ORDER BY f.modified_at DESC
             LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    except Exception:
        return []


def _eskalierte_idvs(db, page=1, page_size=_DEFAULT_PAGE_SIZE):
    """IDVs mit Vollstaendigkeit < 100% UND mind. 4 Erinnerungen erhalten."""
    offset = (page - 1) * page_size
    try:
        return db.execute("""
            SELECT r.id, r.idv_id, r.bezeichnung,
                   COUNT(n.id) AS reminder_count
              FROM idv_register r
              JOIN notification_log n
                ON n.kind   = 'idv_incomplete_reminder'
               AND n.ref_id = r.id
              JOIN v_unvollstaendige_idvs v ON v.idv_id = r.idv_id
             WHERE r.status NOT IN ('Archiviert','Abgekündigt')
               AND NOT EXISTS (
                   SELECT 1 FROM triage_verworfen tv
                    WHERE tv.kategorie = 'eskalierte_idvs'
                      AND tv.ref_key = CAST(r.id AS TEXT)
               )
             GROUP BY r.id, r.idv_id, r.bezeichnung
            HAVING reminder_count >= 4
             ORDER BY reminder_count DESC, r.idv_id
             LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    except Exception:
        return []


def _pool_reminder_ausgelaufen(db, page=1, page_size=_DEFAULT_PAGE_SIZE):
    """Pool-Schritte aelter als ``notify_pool_reminder_max_days`` (Default 14)."""
    offset = (page - 1) * page_size
    try:
        cfg = db.execute(
            "SELECT value FROM app_settings WHERE key='notify_pool_reminder_max_days'"
        ).fetchone()
        max_days = int((cfg["value"] if cfg else "14") or "14")
    except Exception:
        max_days = 14
    try:
        return db.execute(f"""
            SELECT f.id AS freigabe_id, f.schritt, f.beauftragt_am,
                   pool.name AS pool_name, r.id AS idv_db_id,
                   r.idv_id, r.bezeichnung
              FROM idv_freigaben f
              JOIN freigabe_pools pool ON pool.id = f.pool_id
              JOIN idv_register r ON r.id = f.idv_id
             WHERE f.status = 'Ausstehend'
               AND f.pool_id IS NOT NULL
               AND f.zugewiesen_an_id IS NULL
               AND f.beauftragt_am IS NOT NULL
               AND f.beauftragt_am <= datetime('now','-{int(max_days)} days')
               AND NOT EXISTS (
                   SELECT 1 FROM triage_verworfen v
                    WHERE v.kategorie = 'pool_reminder_alt'
                      AND v.ref_key = CAST(f.id AS TEXT)
               )
             ORDER BY f.beauftragt_am
             LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Aggregierter Zaehler (Header-Badge)
# ---------------------------------------------------------------------------

_COUNT_SQL = {
    "mittlere_konfidenz":
     "SELECT COUNT(*) FROM idv_match_suggestions s "
     "JOIN idv_files f ON f.id=s.file_id JOIN idv_register r ON r.id=s.idv_db_id "
     "WHERE s.decision IS NULL AND f.status='active' AND f.bearbeitungsstatus='Neu' "
     "AND r.status NOT IN ('Archiviert') "
     "AND NOT EXISTS (SELECT 1 FROM triage_verworfen v "
     "WHERE v.kategorie='mittlere_konfidenz' AND v.ref_key=CAST(s.id AS TEXT))",
    "owner_fehlt":
     "SELECT COUNT(*) FROM idv_files f "
     "WHERE f.status='active' AND f.bearbeitungsstatus='Neu' "
     "AND f.file_owner IS NOT NULL AND TRIM(f.file_owner)!='' "
     "AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.aktiv=1 "
     "AND (p.user_id=f.file_owner OR p.ad_name=f.file_owner)) "
     "AND NOT EXISTS (SELECT 1 FROM triage_verworfen v "
     "WHERE v.kategorie='owner_fehlt' AND v.ref_key=CAST(f.id AS TEXT))",
    "self_service_stumm":
     "SELECT COUNT(*) FROM self_service_tokens t WHERE t.created_at <= datetime('now','-14 days') "
     "AND t.first_used_at IS NULL AND t.revoked_at IS NULL "
     "AND NOT EXISTS (SELECT 1 FROM self_service_audit a "
     "WHERE a.person_id=t.person_id AND a.created_at >= t.created_at) "
     "AND NOT EXISTS (SELECT 1 FROM triage_verworfen v "
     "WHERE v.kategorie='self_service_stumm' AND v.ref_key=t.jti)",
    "auto_classify_failed":
     "SELECT COUNT(*) FROM idv_files f WHERE f.status='active' "
     "AND f.bearbeitungsstatus='Auto-Klassifizierung fehlgeschlagen' "
     "AND NOT EXISTS (SELECT 1 FROM triage_verworfen v "
     "WHERE v.kategorie='auto_classify_failed' AND v.ref_key=CAST(f.id AS TEXT))",
    "eskalierte_idvs":
     "SELECT COUNT(*) FROM ("
     " SELECT 1 FROM notification_log n "
     " JOIN idv_register r ON r.id=n.ref_id "
     " JOIN v_unvollstaendige_idvs v ON v.idv_id=r.idv_id "
     " WHERE n.kind='idv_incomplete_reminder' "
     " AND r.status NOT IN ('Archiviert','Abgekündigt') "
     " AND NOT EXISTS (SELECT 1 FROM triage_verworfen tv "
     " WHERE tv.kategorie='eskalierte_idvs' AND tv.ref_key=CAST(r.id AS TEXT)) "
     " GROUP BY r.id HAVING COUNT(n.id) >= 4)",
    "pool_reminder_alt":
     "SELECT COUNT(*) FROM idv_freigaben f WHERE f.status='Ausstehend' "
     "AND f.pool_id IS NOT NULL AND f.zugewiesen_an_id IS NULL "
     "AND f.beauftragt_am IS NOT NULL "
     "AND f.beauftragt_am <= datetime('now','-14 days') "
     "AND NOT EXISTS (SELECT 1 FROM triage_verworfen v "
     "WHERE v.kategorie='pool_reminder_alt' AND v.ref_key=CAST(f.id AS TEXT))",
}


def _count_category(db, key: str) -> int:
    sql = _COUNT_SQL.get(key)
    if not sql:
        return 0
    try:
        row = db.execute(sql).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def triage_count(db) -> int:
    """Aggregierte Anzahl Items im Triage-Dashboard (fuer Header-Badge)."""
    return sum(_count_category(db, key) for key in _COUNT_SQL)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

_SECTION_DEFS = [
    ("mittlere_konfidenz", "bi-bullseye", "primary",
     "Mittlere Konfidenz-Treffer",
     "Auto-Match-Vorschläge im mittleren Konfidenz-Bereich — die "
     "Eigentümer entscheiden im Self-Service. Wenn der Vorschlag "
     "offensichtlich nicht passt, hier verwerfen; passt er, kannst du "
     "ihn per »Vorschlag prüfen« direkt übernehmen.",
     "_mittlere_konfidenz"),
    ("owner_fehlt", "bi-person-x", "warning",
     "Owner-Mapping fehlt",
     "Scanner-Dateien, deren Eigentümer-Kennung nicht in der "
     "Personen-Tabelle auflösbar ist.",
     "_owner_mapping_fehlt"),
    ("self_service_stumm", "bi-envelope-slash", "secondary",
     "Self-Service stumm (>14 Tage)",
     "Magic-Link versendet, aber keinerlei Reaktion erfasst.",
     "_self_service_stumm"),
    ("auto_classify_failed", "bi-shuffle", "danger",
     "Auto-Klassifizierung fehlgeschlagen",
     "Auto-Klassifizier-Regel ist getroffen, hat aber kein valides "
     "Ergebnis geliefert.",
     "_auto_classify_fehlgeschlagen"),
    ("eskalierte_idvs", "bi-fire", "danger",
     "Eskalierte IDVs (≥ 4 Erinnerungen, < 100 % vollständig)",
     "Vollständigkeits-Score < 100 % nach 4 oder mehr Reminder-Mails "
     "(vgl. Issue #348).",
     "_eskalierte_idvs"),
    ("pool_reminder_alt", "bi-people", "warning",
     "Pool-Reminder ausgelaufen",
     "Pool-Schritt seit mehr als 14 Tagen offen (konfigurierbar in "
     "den Reminder-Einstellungen).",
     "_pool_reminder_ausgelaufen"),
]


def _page_url(key: str, page: int) -> str:
    """Baut die /triage-URL mit aktualisiertem ``page_<key>`` und
    Anker auf die Sektion. Andere ``page_*``-Parameter bleiben erhalten.
    """
    args = {k: v for k, v in request.args.items()}
    if page <= 1:
        args.pop(f"page_{key}", None)
    else:
        args[f"page_{key}"] = str(page)
    qs = urlencode(args)
    base = url_for("dashboard_triage.index")
    return f"{base}{('?' + qs) if qs else ''}#section-{key}"


def _sort_url(key: str, sort_value: str) -> str:
    """Baut die /triage-URL mit aktualisierter ``sort_<key>``-Wahl
    und resettet ``page_<key>`` (Reihenfolge geaendert -> Seite 1).
    """
    args = {k: v for k, v in request.args.items()}
    args[f"sort_{key}"] = sort_value
    args.pop(f"page_{key}", None)
    qs = urlencode(args)
    base = url_for("dashboard_triage.index")
    return f"{base}{('?' + qs) if qs else ''}#section-{key}"


@bp.route("/triage")
@login_required
@_koordinator_required
def index():
    db = get_db()
    fns = {
        "_mittlere_konfidenz":         _mittlere_konfidenz,
        "_owner_mapping_fehlt":        _owner_mapping_fehlt,
        "_self_service_stumm":         _self_service_stumm,
        "_auto_classify_fehlgeschlagen": _auto_classify_fehlgeschlagen,
        "_eskalierte_idvs":            _eskalierte_idvs,
        "_pool_reminder_ausgelaufen":  _pool_reminder_ausgelaufen,
    }

    per_page = _resolve_page_size()
    sort_mk = request.args.get(
        "sort_mittlere_konfidenz", _DEFAULT_SORT_MITTLERE_KONFIDENZ
    )
    if sort_mk not in _SORTS_MITTLERE_KONFIDENZ:
        sort_mk = _DEFAULT_SORT_MITTLERE_KONFIDENZ

    sections = []
    for key, icon, tone, label, hint, fn_name in _SECTION_DEFS:
        count = _count_category(db, key)
        total_pages = max(1, math.ceil(count / per_page)) if count else 1
        try:
            page = int(request.args.get(f"page_{key}", "1") or "1")
        except (TypeError, ValueError):
            page = 1
        page = max(1, min(page, total_pages))

        fn_kwargs = {"page": page, "page_size": per_page}
        if key == "mittlere_konfidenz":
            fn_kwargs["sort"] = sort_mk
        rows = [dict(r) for r in fns[fn_name](db, **fn_kwargs)]

        section = {
            "key":         key,
            "icon":        icon,
            "tone":        tone,
            "label":       label,
            "hint":        hint,
            "rows":        rows,
            "page":        page,
            "total":       count,
            "total_pages": total_pages,
            "page_size":   per_page,
            "first_url":   _page_url(key, 1) if page > 1 else None,
            "prev_url":    _page_url(key, page - 1) if page > 1 else None,
            "next_url":    _page_url(key, page + 1) if page < total_pages else None,
            "last_url":    _page_url(key, total_pages) if page < total_pages else None,
        }
        if key == "mittlere_konfidenz":
            section["sort"] = sort_mk
            next_sort = ("score_asc"
                         if sort_mk == "score_desc" else "score_desc")
            section["score_sort_url"] = _sort_url(key, next_sort)
        sections.append(section)
    total = sum(s["total"] for s in sections)

    persons_aktiv = []
    has_owner_fehlt = any(
        s["key"] == "owner_fehlt" and s["rows"] for s in sections
    )
    if has_owner_fehlt:
        persons_aktiv = [dict(r) for r in db.execute("""
            SELECT id, nachname, vorname, ad_name, user_id
              FROM persons
             WHERE aktiv = 1
             ORDER BY nachname, vorname
        """).fetchall()]

    return render_template("dashboard_triage.html",
                           sections=sections, total=total,
                           persons_aktiv=persons_aktiv,
                           page_size=per_page,
                           page_size_options=_PAGE_SIZE_OPTIONS)


def _feld_fuer_owner(file_owner: str) -> str:
    """Bestimmt anhand der Schreibweise, in welches Personen-Feld der
    Scanner-file_owner gehoert: ``DOMAIN\\user`` → ``ad_name``,
    sonst ``user_id`` (UPN, Login oder reines Personalkuerzel).
    """
    if "\\" in file_owner:
        return "ad_name"
    return "user_id"


@bp.route("/triage/eigentuemer-zuordnen-bulk", methods=["POST"])
@login_required
@_koordinator_required
def eigentuemer_zuordnen_bulk():
    """Bulk-Variante: mehrere ``file_owner``-Werte in einem Schritt
    Personen zuordnen (parallele Felder ``file_owner[]``/``person_id[]``).
    Eingaben werden zeilenweise validiert, ungueltige Paare verworfen.
    """
    file_owners = request.form.getlist("file_owner")
    person_ids  = request.form.getlist("person_id")

    if not file_owners or len(file_owners) != len(person_ids):
        flash("Bulk-Zuordnung fehlgeschlagen: unvollständige Eingabe.",
              "error")
        return redirect(
            url_for("dashboard_triage.index") + "#section-owner_fehlt"
        )

    db = get_db()
    aktiv_pids = {row["id"] for row in db.execute(
        "SELECT id FROM persons WHERE aktiv = 1"
    ).fetchall()}

    pairs = []
    for fo, pid_raw in zip(file_owners, person_ids):
        fo = (fo or "").strip()
        pid_raw = (pid_raw or "").strip()
        if not fo or not pid_raw:
            continue
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        if pid not in aktiv_pids:
            continue
        pairs.append((_feld_fuer_owner(fo), fo, pid))

    if not pairs:
        flash("Bulk-Zuordnung fehlgeschlagen: keine gültigen Mappings.",
              "error")
        return redirect(
            url_for("dashboard_triage.index") + "#section-owner_fehlt"
        )

    def _do(c):
        with write_tx(c):
            for col, fo, pid in pairs:
                c.execute(
                    f"UPDATE persons SET {col}=? WHERE id=?",
                    (fo, pid),
                )

    get_writer().submit(_do, wait=True)
    flash(f"{len(pairs)} Eigentümer zugeordnet.", "success")
    return redirect(
        url_for("dashboard_triage.index") + "#section-owner_fehlt"
    )


@bp.route("/triage/eigentuemer-zuordnen", methods=["POST"])
@login_required
@_koordinator_required
def eigentuemer_zuordnen():
    """Traegt den vom Scanner gemeldeten ``file_owner``-Wert bei der
    gewaehlten Person ein, damit der Owner-Mapping-Fehlt-Eintrag beim
    naechsten Render verschwindet. Das Zielfeld wird automatisch aus der
    Schreibweise abgeleitet.
    """
    file_owner = (request.form.get("file_owner") or "").strip()
    person_id_raw = (request.form.get("person_id") or "").strip()

    if not file_owner or not person_id_raw:
        flash("Zuordnung fehlgeschlagen: unvollständige Eingabe.", "error")
        return redirect(url_for("dashboard_triage.index"))

    try:
        person_id = int(person_id_raw)
    except ValueError:
        flash("Zuordnung fehlgeschlagen: ungültige Person-ID.", "error")
        return redirect(url_for("dashboard_triage.index"))

    db = get_db()
    person = db.execute(
        "SELECT id, nachname, vorname FROM persons WHERE id=? AND aktiv=1",
        (person_id,),
    ).fetchone()
    if not person:
        flash("Zuordnung fehlgeschlagen: Person nicht gefunden oder inaktiv.", "error")
        return redirect(url_for("dashboard_triage.index"))

    col = _feld_fuer_owner(file_owner)

    def _do(c):
        with write_tx(c):
            c.execute(
                f"UPDATE persons SET {col}=? WHERE id=?",
                (file_owner, person_id),
            )

    get_writer().submit(_do, wait=True)
    flash(
        f"{person['nachname']}, {person['vorname']}: "
        f"{file_owner} als Eigentümer hinterlegt.",
        "success",
    )
    return redirect(url_for("dashboard_triage.index") + "#section-owner_fehlt")


_VALID_KATEGORIEN = frozenset({
    "mittlere_konfidenz",
    "owner_fehlt",
    "self_service_stumm",
    "auto_classify_failed",
    "eskalierte_idvs",
    "pool_reminder_alt",
})

# INSERT…SELECT-Statements fuer Bulk-Verwerfen. Parameter: (verworfen_von_id,)
_BULK_VERWERFEN_SQL = {
    "mittlere_konfidenz": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'mittlere_konfidenz', CAST(s.id AS TEXT), ?
          FROM idv_match_suggestions s
          JOIN idv_files    f ON f.id = s.file_id
          JOIN idv_register r ON r.id = s.idv_db_id
         WHERE s.decision IS NULL AND f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND r.status NOT IN ('Archiviert')
    """,
    "owner_fehlt": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'owner_fehlt', CAST(f.id AS TEXT), ?
          FROM idv_files f
         WHERE f.status = 'active' AND f.bearbeitungsstatus = 'Neu'
           AND f.file_owner IS NOT NULL AND TRIM(f.file_owner) != ''
           AND NOT EXISTS (
               SELECT 1 FROM persons p WHERE p.aktiv = 1
                AND (p.user_id = f.file_owner OR p.ad_name = f.file_owner)
           )
    """,
    "self_service_stumm": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'self_service_stumm', t.jti, ?
          FROM self_service_tokens t
         WHERE t.created_at <= datetime('now','-14 days')
           AND t.first_used_at IS NULL AND t.revoked_at IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM self_service_audit a
                WHERE a.person_id = t.person_id AND a.created_at >= t.created_at
           )
    """,
    "auto_classify_failed": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'auto_classify_failed', CAST(f.id AS TEXT), ?
          FROM idv_files f
         WHERE f.status = 'active'
           AND f.bearbeitungsstatus = 'Auto-Klassifizierung fehlgeschlagen'
    """,
    "eskalierte_idvs": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'eskalierte_idvs', CAST(r.id AS TEXT), ?
          FROM idv_register r
          JOIN notification_log n
            ON n.kind = 'idv_incomplete_reminder' AND n.ref_id = r.id
          JOIN v_unvollstaendige_idvs v ON v.idv_id = r.idv_id
         WHERE r.status NOT IN ('Archiviert','Abgekündigt')
         GROUP BY r.id
        HAVING COUNT(n.id) >= 4
    """,
    "pool_reminder_alt": """
        INSERT OR IGNORE INTO triage_verworfen (kategorie, ref_key, verworfen_von_id)
        SELECT 'pool_reminder_alt', CAST(f.id AS TEXT), ?
          FROM idv_freigaben f
         WHERE f.status = 'Ausstehend' AND f.pool_id IS NOT NULL
           AND f.zugewiesen_an_id IS NULL AND f.beauftragt_am IS NOT NULL
           AND f.beauftragt_am <= datetime('now','-14 days')
    """,
}


@bp.route("/triage/verwerfen", methods=["POST"])
@login_required
@_koordinator_required
def eintrag_verwerfen():
    """Entfernt einen Triage-Eintrag dauerhaft aus der Triage-Ansicht."""
    kategorie = (request.form.get("kategorie") or "").strip()
    ref_key = (request.form.get("ref_key") or "").strip()

    if not kategorie or not ref_key or kategorie not in _VALID_KATEGORIEN:
        flash("Verwerfen fehlgeschlagen: ungültige Eingabe.", "error")
        return redirect(url_for("dashboard_triage.index"))

    person_id = session.get("person_id")

    def _do(c):
        with write_tx(c):
            c.execute(
                "INSERT OR IGNORE INTO triage_verworfen "
                "(kategorie, ref_key, verworfen_von_id) VALUES (?, ?, ?)",
                (kategorie, ref_key, person_id),
            )

    get_writer().submit(_do, wait=True)
    flash("Triage-Eintrag verworfen.", "success")
    return redirect(url_for("dashboard_triage.index") + f"#section-{kategorie}")


@bp.route("/triage/alle-verwerfen", methods=["POST"])
@login_required
@_koordinator_required
def alle_verwerfen():
    """Verwirft alle aktuellen Eintraege einer Triage-Kategorie auf einmal."""
    kategorie = (request.form.get("kategorie") or "").strip()

    if not kategorie or kategorie not in _VALID_KATEGORIEN:
        flash("Verwerfen fehlgeschlagen: ungültige Kategorie.", "error")
        return redirect(url_for("dashboard_triage.index"))

    sql = _BULK_VERWERFEN_SQL[kategorie]
    person_id = session.get("person_id")

    def _do(c):
        with write_tx(c):
            c.execute(sql, (person_id,))

    get_writer().submit(_do, wait=True)
    flash(f'Alle Einträge in "{kategorie}" verworfen.', "success")
    return redirect(url_for("dashboard_triage.index") + f"#section-{kategorie}")


@bp.route("/triage/markierte-verwerfen", methods=["POST"])
@login_required
@_koordinator_required
def markierte_verwerfen():
    """Verwirft die in der Triage-Ansicht markierten Eintraege einer Kategorie."""
    kategorie = (request.form.get("kategorie") or "").strip()
    ref_keys = [k.strip() for k in request.form.getlist("ref_keys")
                if k and k.strip()]

    if not kategorie or kategorie not in _VALID_KATEGORIEN:
        flash("Verwerfen fehlgeschlagen: ungültige Kategorie.", "error")
        return redirect(url_for("dashboard_triage.index"))
    if not ref_keys:
        flash("Keine Einträge ausgewählt.", "warning")
        return redirect(
            url_for("dashboard_triage.index") + f"#section-{kategorie}"
        )

    person_id = session.get("person_id")
    rows = [(kategorie, k, person_id) for k in ref_keys]

    def _do(c):
        with write_tx(c):
            c.executemany(
                "INSERT OR IGNORE INTO triage_verworfen "
                "(kategorie, ref_key, verworfen_von_id) VALUES (?, ?, ?)",
                rows,
            )

    get_writer().submit(_do, wait=True)
    flash(f"{len(ref_keys)} markierte Einträge verworfen.", "success")
    return redirect(
        url_for("dashboard_triage.index") + f"#section-{kategorie}"
    )
