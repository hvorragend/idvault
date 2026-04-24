"""Ausnahmen-Dashboard fuer den IDV-Koordinator (Issue #353).

Zeigt ausschliesslich Items, die menschliche Triage brauchen — alles
andere ist Aufgabe der Automatisierung. Sechs Kategorien, Top-10 je
Kategorie + Direktaktionen.

Sichtbar fuer ROLE_ADMIN und ROLE_KOORDINATOR. Per Helper
``ausnahmen_count(db)`` zur Anzeige der Header-Badge.
"""
from __future__ import annotations

from flask import Blueprint, render_template, redirect, request, url_for, flash, session
from functools import wraps

from . import login_required, get_db, ROLE_ADMIN, ROLE_KOORDINATOR
from ..db_writer import get_writer

from db_write_tx import write_tx


bp = Blueprint("dashboard_ausnahmen", __name__, url_prefix="/dashboard")


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

def _mittlere_konfidenz(db):
    """Auto-Match-Vorschlaege ohne Entscheidung (mittlere Konfidenz)."""
    try:
        return db.execute("""
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
             ORDER BY s.score DESC, s.created_at
             LIMIT 10
        """).fetchall()
    except Exception:
        return []


def _owner_mapping_fehlt(db):
    """Scanner-Dateien mit ``file_owner``, der nicht in ``persons`` aufloesbar ist."""
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
         ORDER BY f.modified_at DESC
         LIMIT 10
    """).fetchall()


def _self_service_stumm(db):
    """Tokens versendet, > 14 Tage ohne Aktion (kein audit-Eintrag fuer den Token)."""
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
             ORDER BY t.created_at
             LIMIT 10
        """).fetchall()
    except Exception:
        return []


def _auto_classify_fehlgeschlagen(db):
    """IDV-Files mit Treffer auf eine Auto-Klassifizier-Regel, aber unklassifiziert."""
    try:
        return db.execute("""
            SELECT f.id AS file_id, f.file_name, f.full_path,
                   f.bearbeitungsstatus, f.modified_at,
                   (SELECT COUNT(*) FROM auto_classify_rules WHERE aktiv=1) AS regel_anzahl
              FROM idv_files f
             WHERE f.status = 'active'
               AND f.bearbeitungsstatus = 'Auto-Klassifizierung fehlgeschlagen'
             ORDER BY f.modified_at DESC
             LIMIT 10
        """).fetchall()
    except Exception:
        return []


def _eskalierte_idvs(db):
    """IDVs mit Vollstaendigkeit < 100% UND mind. 4 Erinnerungen erhalten."""
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
             GROUP BY r.id, r.idv_id, r.bezeichnung
            HAVING reminder_count >= 4
             ORDER BY reminder_count DESC, r.idv_id
             LIMIT 10
        """).fetchall()
    except Exception:
        return []


def _pool_reminder_ausgelaufen(db):
    """Pool-Schritte aelter als ``notify_pool_reminder_max_days`` (Default 14)."""
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
             ORDER BY f.beauftragt_am
             LIMIT 10
        """).fetchall()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Aggregierter Zaehler (Header-Badge)
# ---------------------------------------------------------------------------

_CATEGORIES_FOR_COUNT = (
    ("idv_match_suggestions",
     "SELECT COUNT(*) FROM idv_match_suggestions s "
     "JOIN idv_files f ON f.id=s.file_id JOIN idv_register r ON r.id=s.idv_db_id "
     "WHERE s.decision IS NULL AND f.status='active' AND f.bearbeitungsstatus='Neu' "
     "AND r.status NOT IN ('Archiviert')"),
    ("owner_fehlt",
     "SELECT COUNT(*) FROM idv_files f "
     "WHERE f.status='active' AND f.bearbeitungsstatus='Neu' "
     "AND f.file_owner IS NOT NULL AND TRIM(f.file_owner)!='' "
     "AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.aktiv=1 "
     "AND (p.user_id=f.file_owner OR p.ad_name=f.file_owner))"),
    ("self_service_stumm",
     "SELECT COUNT(*) FROM self_service_tokens t WHERE t.created_at <= datetime('now','-14 days') "
     "AND t.first_used_at IS NULL AND t.revoked_at IS NULL "
     "AND NOT EXISTS (SELECT 1 FROM self_service_audit a "
     "WHERE a.person_id=t.person_id AND a.created_at >= t.created_at)"),
    ("auto_classify_failed",
     "SELECT COUNT(*) FROM idv_files f WHERE f.status='active' "
     "AND f.bearbeitungsstatus='Auto-Klassifizierung fehlgeschlagen'"),
    ("eskalierte_idvs",
     "SELECT COUNT(*) FROM ("
     " SELECT 1 FROM notification_log n "
     " JOIN idv_register r ON r.id=n.ref_id "
     " JOIN v_unvollstaendige_idvs v ON v.idv_id=r.idv_id "
     " WHERE n.kind='idv_incomplete_reminder' "
     " AND r.status NOT IN ('Archiviert','Abgekündigt') "
     " GROUP BY r.id HAVING COUNT(n.id) >= 4)"),
    ("pool_reminder_alt",
     "SELECT COUNT(*) FROM idv_freigaben f WHERE f.status='Ausstehend' "
     "AND f.pool_id IS NOT NULL AND f.zugewiesen_an_id IS NULL "
     "AND f.beauftragt_am IS NOT NULL "
     "AND f.beauftragt_am <= datetime('now','-14 days')"),
)


def ausnahmen_count(db) -> int:
    """Aggregierte Anzahl Items im Ausnahmen-Dashboard (fuer Header-Badge)."""
    total = 0
    for _name, sql in _CATEGORIES_FOR_COUNT:
        try:
            row = db.execute(sql).fetchone()
            total += int(row[0] or 0)
        except Exception:
            continue
    return total


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@bp.route("/ausnahmen")
@login_required
@_koordinator_required
def index():
    db = get_db()
    sections = [
        {
            "key":   "mittlere_konfidenz",
            "icon":  "bi-bullseye",
            "tone":  "primary",
            "label": "Mittlere Konfidenz-Treffer",
            "hint":  "Auto-Match-Vorschläge, die der Owner per Self-Service "
                     "bestätigen oder ablehnen muss.",
            "rows":  [dict(r) for r in _mittlere_konfidenz(db)],
        },
        {
            "key":   "owner_fehlt",
            "icon":  "bi-person-x",
            "tone":  "warning",
            "label": "Owner-Mapping fehlt",
            "hint":  "Scanner-Dateien mit ``file_owner``, der nicht in der "
                     "Personen-Tabelle auflösbar ist.",
            "rows":  [dict(r) for r in _owner_mapping_fehlt(db)],
        },
        {
            "key":   "self_service_stumm",
            "icon":  "bi-envelope-slash",
            "tone":  "secondary",
            "label": "Self-Service stumm (>14 Tage)",
            "hint":  "Magic-Link versendet, aber keinerlei Reaktion erfasst.",
            "rows":  [dict(r) for r in _self_service_stumm(db)],
        },
        {
            "key":   "auto_classify_failed",
            "icon":  "bi-shuffle",
            "tone":  "danger",
            "label": "Auto-Klassifizierung fehlgeschlagen",
            "hint":  "Auto-Klassifizier-Regel ist getroffen, hat aber kein "
                     "valides Ergebnis geliefert.",
            "rows":  [dict(r) for r in _auto_classify_fehlgeschlagen(db)],
        },
        {
            "key":   "eskalierte_idvs",
            "icon":  "bi-fire",
            "tone":  "danger",
            "label": "Eskalierte IDVs (≥ 4 Erinnerungen, < 100 % vollständig)",
            "hint":  "Vollständigkeits-Score < 100 % nach 4 oder mehr "
                     "Reminder-Mails (vgl. Issue #348).",
            "rows":  [dict(r) for r in _eskalierte_idvs(db)],
        },
        {
            "key":   "pool_reminder_alt",
            "icon":  "bi-people",
            "tone":  "warning",
            "label": "Pool-Reminder ausgelaufen",
            "hint":  "Pool-Schritt offen länger als "
                     "``notify_pool_reminder_max_days`` (Default 14 Tage).",
            "rows":  [dict(r) for r in _pool_reminder_ausgelaufen(db)],
        },
    ]
    total = sum(len(s["rows"]) for s in sections)

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

    return render_template("dashboard_ausnahmen.html",
                           sections=sections, total=total,
                           persons_aktiv=persons_aktiv)


def _feld_fuer_owner(file_owner: str) -> str:
    """Bestimmt anhand der Schreibweise, in welches Personen-Feld der
    Scanner-file_owner gehoert: ``DOMAIN\\user`` → ``ad_name``,
    sonst ``user_id`` (UPN, Login oder reines Personalkuerzel).
    """
    if "\\" in file_owner:
        return "ad_name"
    return "user_id"


@bp.route("/ausnahmen/eigentuemer-zuordnen", methods=["POST"])
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
        return redirect(url_for("dashboard_ausnahmen.index"))

    try:
        person_id = int(person_id_raw)
    except ValueError:
        flash("Zuordnung fehlgeschlagen: ungültige Person-ID.", "error")
        return redirect(url_for("dashboard_ausnahmen.index"))

    db = get_db()
    person = db.execute(
        "SELECT id, nachname, vorname FROM persons WHERE id=? AND aktiv=1",
        (person_id,),
    ).fetchone()
    if not person:
        flash("Zuordnung fehlgeschlagen: Person nicht gefunden oder inaktiv.", "error")
        return redirect(url_for("dashboard_ausnahmen.index"))

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
    return redirect(url_for("dashboard_ausnahmen.index") + "#section-owner_fehlt")
