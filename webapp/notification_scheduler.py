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
    "notify_schedule_enabled": "1",
    "notify_schedule_time":    "08:00",
}

_MEASURE_KIND = "massnahme_ueberfaellig"
_REVIEW_KIND  = "pruefung_faellig"

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


def _run_daily_dispatch(app) -> None:
    """Einmaliger Durchlauf für den aktuellen Tag (idempotent dank Dedup)."""
    with app.app_context():
        db = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        m_sent = _dispatch_overdue_measures(db, today)
        r_sent = _dispatch_due_reviews(db, today)
        log.info(
            "Notification-Dispatch abgeschlossen: %d Maßnahmen, %d Prüfungen.",
            m_sent, r_sent,
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
                                _run_daily_dispatch(app)
                                _last_triggered_in_memory = today_str

                                def _mark(c, _today=today_str):
                                    with write_tx(c):
                                        c.execute(
                                            "INSERT OR REPLACE INTO app_settings "
                                            "(key, value) VALUES "
                                            "('notify_last_triggered_date', ?)",
                                            (_today,)
                                        )
                                get_writer().submit(_mark, wait=True)
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
            "massnahmen": _dispatch_overdue_measures(db, today),
            "pruefungen": _dispatch_due_reviews(db, today),
        }
