"""
idvault – Notification-Scheduler (Fristenueberwachung)
======================================================

Daemon-Thread, der einmal taeglich prueft, ob Maßnahmen oder Prüfungen
überfällig sind, und via email_service die zugehörigen Verantwortlichen
benachrichtigt.

Pattern angelehnt an webapp/routes/admin.py (_scheduler_loop):
- eigenes Modul, damit admin.py nicht weiter wächst
- Konfiguration in app_settings (notify_schedule_enabled,
  notify_schedule_time, notify_last_triggered_date)
- Dedup pro Datensatz via notification_log-Tabelle (7-Tage-Fenster)
- Writes ausschließlich über den Single-Writer-Thread (get_writer)
"""

from __future__ import annotations

import sys
import os
import threading
import time
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db_write_tx import write_tx
from .db_flask import get_db
from .db_writer import get_writer

log = logging.getLogger(__name__)


_NOTIFY_DEFAULTS = {
    "notify_schedule_enabled":       "1",
    "notify_schedule_time":          "08:00",
    "notify_pool_reminder_max_days": "14",
    "self_service_enabled":          "0",
    "self_service_frequency_days":   "7",
}

_MEASURE_KIND       = "massnahme_ueberfaellig"
_REVIEW_KIND        = "pruefung_faellig"
_POOL_REMINDER_KIND  = "freigabe_pool_reminder"
_OWNER_DIGEST_KIND   = "owner_digest"

# Anti-Spam: gleiche (kind, ref_id) wird innerhalb dieses Fensters nicht
# erneut gemailt – auch wenn die Fälligkeit weiter in der Vergangenheit liegt.
_DEDUP_WINDOW_DAYS = 7

_scheduler_thread_obj: threading.Thread = None
_last_triggered_in_memory: str = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_notification_settings(db) -> dict:
    cfg = dict(_NOTIFY_DEFAULTS)
    rows = db.execute(
        "SELECT key, value FROM app_settings WHERE key LIKE 'notify_%'"
    ).fetchall()
    for r in rows:
        cfg[r["key"]] = r["value"]
    return cfg


# ---------------------------------------------------------------------------
# Dedup-Helfer
# ---------------------------------------------------------------------------

def _recent_sent(db, kind: str, ref_id: int) -> bool:
    """Gibt True zurück, wenn für (kind, ref_id) innerhalb der Dedup-Fensters
    bereits eine Benachrichtigung protokolliert wurde."""
    row = db.execute(
        "SELECT 1 FROM notification_log "
        "WHERE kind=? AND ref_id=? AND sent_date >= date('now', ?)",
        (kind, ref_id, f"-{_DEDUP_WINDOW_DAYS} days"),
    ).fetchone()
    return row is not None


def _record_sent(kind: str, ref_id: int, today_iso: str) -> None:
    def _do(c):
        with write_tx(c):
            c.execute(
                "INSERT OR IGNORE INTO notification_log (kind, ref_id, sent_date) "
                "VALUES (?, ?, ?)",
                (kind, ref_id, today_iso),
            )
    get_writer().submit(_do, wait=True)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _effective_email(db, person_id: int, direct_email: str) -> str:
    """Gibt die E-Mail-Adresse zurück, an die benachrichtigt werden soll.

    Wenn die Person aktuell abwesend ist und einen aktiven Stellvertreter hat,
    wird dessen E-Mail-Adresse zurückgegeben – andernfalls die direkte Adresse.
    """
    try:
        row = db.execute("""
            SELECT p2.email AS sv_email
            FROM persons p
            JOIN persons p2 ON p2.id = p.stellvertreter_id
            WHERE p.id = ?
              AND p.abwesend_bis IS NOT NULL
              AND p.abwesend_bis >= date('now')
              AND p2.aktiv = 1
              AND p2.email IS NOT NULL AND p2.email <> ''
        """, (person_id,)).fetchone()
        if row:
            return row["sv_email"]
    except Exception:
        pass
    return direct_email


def _dispatch_overdue_measures(db, today_iso: str) -> int:
    """Mailt überfällige Maßnahmen an den Verantwortlichen (oder dessen
    aktiven Stellvertreter). Gibt Anzahl versandter Mails zurück."""
    from .email_service import notify_measure_overdue

    rows = db.execute("""
        SELECT m.id AS id, m.titel, m.faellig_am,
               p.id AS person_id, p.email
        FROM massnahmen m
        JOIN persons p ON m.verantwortlicher_id = p.id
        WHERE m.status IN ('Offen','In Bearbeitung')
          AND m.faellig_am IS NOT NULL
          AND m.faellig_am < date('now')
          AND p.aktiv = 1
          AND p.email IS NOT NULL AND p.email <> ''
    """).fetchall()

    sent = 0
    for r in rows:
        if _recent_sent(db, _MEASURE_KIND, r["id"]):
            continue
        email = _effective_email(db, r["person_id"], r["email"])
        try:
            ok = notify_measure_overdue(db, r, email)
        except Exception:
            log.exception("Fehler beim Versand Maßnahmen-Erinnerung (id=%s)", r["id"])
            ok = False
        if ok:
            _record_sent(_MEASURE_KIND, r["id"], today_iso)
            sent += 1
    return sent


def _dispatch_due_reviews(db, today_iso: str) -> int:
    """Mailt Prüfungen, deren Fälligkeit überschritten ist, an den
    Fachverantwortlichen (oder dessen aktiven Stellvertreter).
    Gibt Anzahl versandter Mails zurück."""
    from .email_service import notify_review_due

    rows = db.execute("""
        SELECT r.id AS id, r.idv_id, r.bezeichnung, r.naechste_pruefung,
               p.id AS person_id, p.email
        FROM idv_register r
        JOIN persons p ON r.fachverantwortlicher_id = p.id
        WHERE r.naechste_pruefung IS NOT NULL
          AND r.naechste_pruefung < date('now')
          AND (r.status IS NULL OR r.status NOT IN ('Archiviert'))
          AND p.aktiv = 1
          AND p.email IS NOT NULL AND p.email <> ''
    """).fetchall()

    sent = 0
    for r in rows:
        if _recent_sent(db, _REVIEW_KIND, r["id"]):
            continue
        email = _effective_email(db, r["person_id"], r["email"])
        try:
            ok = notify_review_due(db, r, email)
        except Exception:
            log.exception("Fehler beim Versand Prüfungs-Erinnerung (id=%s)", r["id"])
            ok = False
        if ok:
            _record_sent(_REVIEW_KIND, r["id"], today_iso)
            sent += 1
    return sent


def _dispatch_pool_claim_reminders(db, today_iso: str) -> int:
    """Mailt tägliche Erinnerung an Pool-Mitglieder für noch nicht geclaimte
    Freigabe-Schritte. Gibt Anzahl versandter Mails zurück.

    Regeln (Issue #321, Akzeptanzkriterien):
    - Nur offene Pool-Schritte ohne aktiven Claim (``bearbeitet_von_id IS NULL``).
    - Pool muss ≥ 2 aktive Mitglieder mit E-Mail haben — 0/1-Mitglied-Pools
      erzeugen keine Mail-Flut.
    - Reminder läuft höchstens ``notify_pool_reminder_max_days`` Tage ab
      Anlage des Schritts. Danach: stumm (damit Mails nicht endlos laufen).
    - Dedup pro Tag via notification_log-Eintrag (kind + ref_id + sent_date).
    - Sobald ein Mitglied claimed, entfällt der Reminder für alle anderen —
      direkt durch das ``bearbeitet_von_id IS NULL``-Filter.
    """
    from .email_service import notify_freigabe_pool_reminder, get_app_base_url
    from .tokens import make_freigabe_token
    from flask import current_app

    max_days_row = db.execute(
        "SELECT value FROM app_settings WHERE key='notify_pool_reminder_max_days'"
    ).fetchone()
    try:
        max_days = int(max_days_row["value"]) if max_days_row else 14
    except (TypeError, ValueError):
        max_days = 14

    rows = db.execute("""
        SELECT f.id AS freigabe_id, f.schritt, f.beauftragt_am,
               r.id AS idv_db_id, r.idv_id, r.bezeichnung, r.idv_entwickler_id,
               pool.id AS pool_id, pool.name AS pool_name,
               CAST(julianday('now') - julianday(substr(f.beauftragt_am,1,10))
                    AS INTEGER) AS wartet_tage
        FROM idv_freigaben f
        JOIN idv_register r     ON r.id  = f.idv_id
        JOIN freigabe_pools pool ON pool.id = f.pool_id
        WHERE f.status = 'Ausstehend'
          AND f.pool_id IS NOT NULL
          AND f.bearbeitet_von_id IS NULL
          AND f.beauftragt_am IS NOT NULL
          AND julianday('now') - julianday(substr(f.beauftragt_am,1,10)) <= ?
          AND pool.aktiv = 1
    """, (max_days,)).fetchall()

    if not rows:
        return 0

    secret_key = current_app.config.get("SECRET_KEY", "")
    base_url = get_app_base_url(db)

    sent = 0
    for r in rows:
        if _recent_sent(db, _POOL_REMINDER_KIND, r["freigabe_id"]):
            continue

        members = db.execute("""
            SELECT p.email FROM freigabe_pool_members m
            JOIN persons p ON p.id = m.person_id
            WHERE m.pool_id = ?
              AND p.aktiv = 1
              AND p.email IS NOT NULL AND p.email <> ''
              AND p.id != ?
        """, (r["pool_id"], r["idv_entwickler_id"] or 0)).fetchall()

        emails = [m["email"] for m in members if m["email"]]
        # 0/1 Mitglied(er): keine Mail-Flut (Akzeptanzkriterium)
        if len(emails) < 2:
            continue

        action_url = None
        if base_url and secret_key:
            try:
                token = make_freigabe_token(secret_key, r["freigabe_id"])
                action_url = f"{base_url}/quick/freigabe/{r['freigabe_id']}?token={token}"
            except Exception:
                action_url = None

        try:
            ok = notify_freigabe_pool_reminder(
                db, r, r["schritt"], r["pool_name"],
                int(r["wartet_tage"] or 0), emails,
                action_url=action_url,
            )
        except Exception:
            log.exception(
                "Fehler beim Versand Pool-Reminder (freigabe_id=%s)",
                r["freigabe_id"],
            )
            ok = False
        if ok:
            _record_sent(_POOL_REMINDER_KIND, r["freigabe_id"], today_iso)
            sent += 1
    return sent


def _self_service_master_enabled(db) -> bool:
    """Self-Service greift nur, wenn der Admin-UI-Schalter
    ``app_settings.self_service_enabled`` gesetzt ist (Default: aus,
    siehe Issue #315)."""
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key='self_service_enabled'"
        ).fetchone()
    except Exception:
        return False
    return bool(row and row["value"] == "1")


def _dispatch_owner_digest(db, today_iso: str) -> int:
    """Wöchentlicher Owner-Digest: gruppiert neue Scanner-Funde nach
    Fachbereichs-Mitarbeiter (aus ``file_owner`` → ``persons`` resolved)
    und sendet pro Empfänger **höchstens eine** Digest-Mail innerhalb des
    konfigurierten Intervalls.

    Greift nur, wenn ``_self_service_master_enabled`` True liefert.
    """
    if not _self_service_master_enabled(db):
        return 0

    from .email_service import notify_owner_digest, get_app_base_url
    from .tokens import make_self_service_token
    from flask import current_app
    import secrets as _secrets

    # Dedup-Fenster aus self_service_frequency_days (mind. 1 Tag).
    try:
        freq_row = db.execute(
            "SELECT value FROM app_settings WHERE key='self_service_frequency_days'"
        ).fetchone()
        freq_days = max(1, int(freq_row["value"])) if freq_row else 7
    except Exception:
        freq_days = 7

    # Offene Funde gruppieren: file_owner ↔ persons (user_id | kuerzel | ad_name)
    rows = db.execute("""
        SELECT f.id, f.file_name, f.full_path, f.file_owner,
               p.id     AS person_id,
               p.email  AS email,
               TRIM(COALESCE(p.vorname,'') || ' ' || COALESCE(p.nachname,''))
                       AS anzeigename
          FROM idv_files f
          JOIN persons p
            ON p.aktiv = 1
           AND p.email IS NOT NULL AND p.email <> ''
           AND (
                 LOWER(p.user_id) = LOWER(f.file_owner)
              OR LOWER(p.kuerzel) = LOWER(f.file_owner)
              OR LOWER(p.ad_name) = LOWER(f.file_owner)
               )
         WHERE f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND f.file_owner IS NOT NULL AND f.file_owner <> ''
           AND NOT EXISTS (SELECT 1 FROM idv_register r   WHERE r.file_id = f.id)
           AND NOT EXISTS (SELECT 1 FROM idv_file_links l WHERE l.file_id = f.id)
    """).fetchall()

    if not rows:
        return 0

    grouped: dict[int, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["person_id"], {
            "person_id":  r["person_id"],
            "email":      r["email"],
            "anzeigename": r["anzeigename"] or r["email"],
            "files":      [],
        })
        g["files"].append(r)

    secret_key = current_app.config.get("SECRET_KEY", "")
    base_url   = get_app_base_url(db)
    if not base_url or not secret_key:
        log.warning(
            "Owner-Digest übersprungen: app_base_url oder SECRET_KEY fehlt."
        )
        return 0

    sent = 0
    expires_at_iso = (
        datetime.utcnow().replace(microsecond=0).isoformat(" ")
    )  # Platzhalter – wird unten überschrieben
    for person_id, group in grouped.items():
        # Dedup pro Empfänger + Intervall
        recent = db.execute(
            "SELECT 1 FROM notification_log "
            "WHERE kind=? AND ref_id=? AND sent_date >= date('now', ?)",
            (_OWNER_DIGEST_KIND, person_id, f"-{freq_days} days"),
        ).fetchone()
        if recent:
            continue

        jti = _secrets.token_urlsafe(18)
        try:
            token = make_self_service_token(secret_key, person_id, jti)
        except Exception:
            log.exception("Token-Erzeugung fehlgeschlagen (person_id=%s)", person_id)
            continue

        # Token serverseitig registrieren (7 Tage gültig – siehe tokens.py).
        # expires_at für Transparenz in der Tabelle, die Signatur ist autoritativ.
        from datetime import timedelta as _td
        expires_at = (datetime.utcnow() + _td(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        def _register(c, _jti=jti, _pid=person_id, _exp=expires_at):
            with write_tx(c):
                c.execute(
                    "INSERT INTO self_service_tokens "
                    "(jti, person_id, expires_at) VALUES (?,?,?)",
                    (_jti, _pid, _exp),
                )
        try:
            get_writer().submit(_register, wait=True)
        except Exception:
            log.exception("Token-Registrierung in DB fehlgeschlagen (jti=%s)", jti)
            continue

        magic_link = f"{base_url}/selbst/meine-funde?token={token}"

        try:
            ok = notify_owner_digest(
                db,
                recipient_email=group["email"],
                recipient_name=group["anzeigename"],
                file_rows=group["files"],
                magic_link=magic_link,
                base_url=base_url,
            )
        except Exception:
            log.exception("Fehler beim Versand Owner-Digest (person_id=%s)", person_id)
            ok = False

        if ok:
            _record_sent(_OWNER_DIGEST_KIND, person_id, today_iso)
            sent += 1
        else:
            # Versand nicht erfolgreich → Token direkt widerrufen,
            # damit im Fehlerfall keine "Waisen-Tokens" stehen bleiben.
            def _revoke(c, _jti=jti):
                with write_tx(c):
                    c.execute(
                        "UPDATE self_service_tokens "
                        "SET revoked_at = datetime('now','utc') "
                        "WHERE jti = ?",
                        (_jti,),
                    )
            try:
                get_writer().submit(_revoke, wait=True)
            except Exception:
                pass

    return sent


def _run_daily_dispatch(app) -> None:
    """Einmaliger Durchlauf für den aktuellen Tag (idempotent dank Dedup)."""
    with app.app_context():
        db = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        m_sent = _dispatch_overdue_measures(db, today)
        r_sent = _dispatch_due_reviews(db, today)
        p_sent = _dispatch_pool_claim_reminders(db, today)
        o_sent = _dispatch_owner_digest(db, today)
        log.info(
            "Notification-Dispatch abgeschlossen: %d Maßnahmen, %d Prüfungen, "
            "%d Pool-Reminder, %d Owner-Digest.",
            m_sent, r_sent, p_sent, o_sent,
        )


# ---------------------------------------------------------------------------
# Scheduler-Loop
# ---------------------------------------------------------------------------

def _notification_loop(app) -> None:
    """Daemon-Thread: prüft jede Minute ob der tägliche Dispatch fällig ist."""
    global _last_triggered_in_memory

    time.sleep(30)  # App soll vollständig hochgefahren sein

    while True:
        try:
            with app.app_context():
                db  = get_db()
                cfg = _load_notification_settings(db)

                if cfg.get("notify_schedule_enabled") != "1":
                    pass
                else:
                    now = datetime.now()
                    today_str = now.strftime("%Y-%m-%d")

                    if _last_triggered_in_memory != today_str:
                        db_row = db.execute(
                            "SELECT value FROM app_settings "
                            "WHERE key='notify_last_triggered_date'"
                        ).fetchone()
                        db_last = db_row["value"] if db_row else None

                        if db_last != today_str:
                            try:
                                h, m = map(int, cfg["notify_schedule_time"].split(":"))
                            except Exception:
                                h, m = 8, 0

                            if now.hour > h or (now.hour == h and now.minute >= m):
                                # Persist-first: bei Crash zwischen DB-Write und
                                # Dispatch entfällt der Lauf — das ist harmloser als
                                # ein Doppelversand nach Neustart.
                                def _mark(c, _today=today_str):
                                    with write_tx(c):
                                        c.execute(
                                            "INSERT OR REPLACE INTO app_settings "
                                            "(key, value) VALUES "
                                            "('notify_last_triggered_date', ?)",
                                            (_today,)
                                        )
                                get_writer().submit(_mark, wait=True)
                                _last_triggered_in_memory = today_str
                                _run_daily_dispatch(app)
        except Exception:
            try:
                app.logger.exception("Fehler im Notification-Scheduler")
            except Exception:
                pass

        time.sleep(60)


def start_notification_scheduler(app) -> None:
    """Startet den Notification-Daemon-Thread (idempotent)."""
    global _scheduler_thread_obj
    if _scheduler_thread_obj is not None and _scheduler_thread_obj.is_alive():
        return
    t = threading.Thread(
        target=_notification_loop,
        args=(app,),
        daemon=True,
        name="idvault-notification-scheduler",
    )
    _scheduler_thread_obj = t
    t.start()


def trigger_now(app) -> dict:
    """Manueller Trigger (z.B. aus Admin-UI oder CLI).

    Läuft synchron im aufrufenden Thread, gibt die Anzahlen als Dict zurück.
    Dedup verhindert doppelten Versand, falls heute bereits gelaufen.
    """
    with app.app_context():
        db    = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "massnahmen":    _dispatch_overdue_measures(db, today),
            "pruefungen":    _dispatch_due_reviews(db, today),
            "pool_reminder": _dispatch_pool_claim_reminders(db, today),
            "owner_digest":  _dispatch_owner_digest(db, today),
        }
