"""
IDVScope – Notification-Scheduler (Fristenueberwachung)
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
    # Sofort-Schwelle für die Sammelbenachrichtigung an Owner (Issue #346):
    # Ab dieser Anzahl offener Scanner-Funde pro Empfänger wird die
    # Sammelbenachrichtigung sofort gesendet – auch wenn das reguläre
    # Intervall noch nicht erreicht ist. ``0`` deaktiviert den Sofort-Versand
    # (altes Verhalten).
    "owner_digest_burst_threshold":  "25",
    # Issue #355: dreistufige Eskalations-Automatik
    "escalation_reminder_days":        "7",
    "escalation_to_lead_days":         "14",
    "escalation_to_coordinator_days":  "21",
}

_MEASURE_KIND       = "massnahme_ueberfaellig"
_REVIEW_KIND        = "pruefung_faellig"
_POOL_REMINDER_KIND  = "freigabe_pool_reminder"
_OWNER_DIGEST_KIND   = "owner_digest"
_IDV_INCOMPLETE_KIND = "idv_incomplete_reminder"
# Issue #355: Eskalations-Kinds (eine Kind je Stufe + Owner)
_ESC_REMIND_OWNER     = "self_service_remind_owner"
_ESC_TO_OE_LEAD       = "self_service_escalate_oe_lead"
_ESC_TO_COORDINATOR   = "self_service_escalate_coordinator"

# Anti-Spam: gleiche (kind, ref_id) wird innerhalb dieses Fensters nicht
# erneut gemailt – auch wenn die Fälligkeit weiter in der Vergangenheit liegt.
_DEDUP_WINDOW_DAYS = 7

# Maximale Anzahl Erinnerungen für unvollständige IDVs (Issue #348): danach
# verstummt der Reminder, um Mail-Flut zu vermeiden — die IDV ist dann
# weiterhin in der Liste unvollständiger IDVs sichtbar.
_IDV_INCOMPLETE_MAX_REMINDERS = 4

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
    """Mailt überfällige Maßnahmen an die in der Vorlage konfigurierten
    Empfänger-Rollen (Maßnahmen-Verantwortlicher und/oder
    Koordinatoren/Admins). Gibt Anzahl versandter Mails zurück."""
    from .email_service import (
        notify_measure_overdue,
        get_configured_recipient_roles,
    )

    active_roles = set(get_configured_recipient_roles(db, "massnahme_ueberfaellig"))
    if not active_roles:
        return 0

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

    cc_emails: set[str] = set()
    if "idv_administrator" in active_roles or "idv_koordinator" in active_roles:
        wanted = []
        if "idv_administrator" in active_roles:
            wanted.append("IDV-Administrator")
        if "idv_koordinator" in active_roles:
            wanted.append("IDV-Koordinator")
        placeholders = ",".join("?" * len(wanted))
        for cc in db.execute(
            f"SELECT email FROM persons WHERE aktiv=1 "
            f"AND email IS NOT NULL AND email <> '' AND rolle IN ({placeholders})",
            wanted,
        ).fetchall():
            cc_emails.add(cc["email"])

    sent = 0
    for r in rows:
        if _recent_sent(db, _MEASURE_KIND, r["id"]):
            continue
        emails: set[str] = set(cc_emails)
        if "massnahme_verantwortlicher" in active_roles:
            emails.add(_effective_email(db, r["person_id"], r["email"]))
        emails = {e for e in emails if e and "@" in e}
        if not emails:
            continue
        try:
            ok = notify_measure_overdue(db, r, sorted(emails))
        except Exception:
            log.exception("Fehler beim Versand Maßnahmen-Erinnerung (id=%s)", r["id"])
            ok = False
        if ok:
            _record_sent(_MEASURE_KIND, r["id"], today_iso)
            sent += 1
    return sent


def _dispatch_due_reviews(db, today_iso: str) -> int:
    """Mailt Prüfungen, deren Fälligkeit überschritten ist, an die in der
    Vorlage konfigurierten Empfänger-Rollen (Fachverantwortlicher und/oder
    Entwickler/Koordinatoren/Admins). Gibt Anzahl versandter Mails zurück."""
    from .email_service import (
        notify_review_due,
        get_configured_recipient_roles,
    )

    active_roles = set(get_configured_recipient_roles(db, "pruefung_faellig"))
    if not active_roles:
        return 0

    rows = db.execute("""
        SELECT r.id AS id, r.idv_id, r.bezeichnung, r.naechste_pruefung,
               r.idv_entwickler_id, r.fachverantwortlicher_id,
               p.id AS person_id, p.email
        FROM idv_register r
        JOIN persons p ON r.fachverantwortlicher_id = p.id
        WHERE r.naechste_pruefung IS NOT NULL
          AND r.naechste_pruefung < date('now')
          AND (r.status IS NULL OR r.status NOT IN ('Archiviert'))
          AND p.aktiv = 1
          AND p.email IS NOT NULL AND p.email <> ''
    """).fetchall()

    cc_emails: set[str] = set()
    if "idv_administrator" in active_roles or "idv_koordinator" in active_roles:
        wanted = []
        if "idv_administrator" in active_roles:
            wanted.append("IDV-Administrator")
        if "idv_koordinator" in active_roles:
            wanted.append("IDV-Koordinator")
        placeholders = ",".join("?" * len(wanted))
        for cc in db.execute(
            f"SELECT email FROM persons WHERE aktiv=1 "
            f"AND email IS NOT NULL AND email <> '' AND rolle IN ({placeholders})",
            wanted,
        ).fetchall():
            cc_emails.add(cc["email"])

    sent = 0
    for r in rows:
        if _recent_sent(db, _REVIEW_KIND, r["id"]):
            continue
        emails: set[str] = set(cc_emails)
        if "fachverantwortlicher" in active_roles:
            emails.add(_effective_email(db, r["person_id"], r["email"]))
        if "idv_entwickler" in active_roles and r["idv_entwickler_id"]:
            ent = db.execute(
                "SELECT id, email FROM persons WHERE id=? AND aktiv=1 "
                "AND email IS NOT NULL AND email <> ''",
                (r["idv_entwickler_id"],),
            ).fetchone()
            if ent:
                emails.add(_effective_email(db, ent["id"], ent["email"]))
        emails = {e for e in emails if e and "@" in e}
        if not emails:
            continue
        try:
            ok = notify_review_due(db, r, sorted(emails))
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
    from .email_service import (
        notify_freigabe_pool_reminder,
        get_app_base_url,
        get_configured_recipient_roles,
    )
    from .tokens import make_freigabe_token
    from flask import current_app

    if "freigabe_pool" not in set(
        get_configured_recipient_roles(db, "freigabe_pool_reminder")
    ):
        return 0

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


def _dispatch_owner_digest(
    db,
    today_iso: str,
    *,
    force: bool = False,
    test_recipient: str | None = None,
    test_limit: int = 3,
) -> dict:
    """Wöchentliche Sammelbenachrichtigung an Fachbereichs-Mitarbeiter:
    gruppiert neue Scanner-Funde nach Empfänger (aus ``file_owner`` →
    ``persons`` resolved) und sendet pro Empfänger **höchstens eine** Mail
    innerhalb des konfigurierten Intervalls.

    Ab ``owner_digest_burst_threshold`` offenen Funden pro Empfänger wird
    das Intervall ignoriert und sofort versendet (Sofort-Schwelle,
    Issue #346). Ein hartes Tageslimit verhindert auch in diesem Fall
    mehr als eine Mail pro Empfänger und Tag.

    Greift nur, wenn ``_self_service_master_enabled`` True liefert.

    ``force=True`` (manueller Sofortversand aus dem Admin-UI) ignoriert
    Tageslimit und Intervall-Dedup, registriert Tokens und protokolliert
    den Versand in ``notification_log`` wie ein regulärer Lauf.

    ``test_recipient`` (Testversand aus dem Admin-UI) leitet alle Mails
    an diese Adresse um, kennzeichnet Betreff/Body als Test, ignoriert
    Master-Switch und Dedup-Gates, erzeugt keine Tokens und schreibt
    nicht in ``notification_log``. Begrenzt auf ``test_limit`` Empfänger
    (Default 3), damit ein einzelner Test keine Mailflut auslöst.

    Rückgabe: ``{"sent": int, "candidates": [...], "skipped_test_limit": int}``.
    ``candidates`` enthält pro Eintrag ``email``, ``name``, ``file_count``
    und ``burst`` für Anzeige im Admin-UI.
    """
    test_mode = bool(test_recipient)
    if not test_mode and not _self_service_master_enabled(db):
        return {"sent": 0, "candidates": [], "skipped_test_limit": 0}

    from .email_service import (
        notify_owner_digest, get_app_base_url,
        get_configured_recipient_roles,
    )

    # Self-Service-Empfaenger als Rolle konfigurierbar — wenn deaktiviert,
    # geht trotz Master-Switch keine Owner-Mail raus.
    if "self_service_owner" not in set(
        get_configured_recipient_roles(db, "owner_digest")
    ):
        return {"sent": 0, "candidates": [], "skipped_test_limit": 0}
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

    # Sofort-Schwelle: bei ≥ N offenen Funden pro Empfänger wird die
    # Sammelbenachrichtigung sofort gesendet, auch wenn das Intervall
    # (freq_days) noch läuft. 0 deaktiviert den Sofort-Versand.
    try:
        burst_row = db.execute(
            "SELECT value FROM app_settings WHERE key='owner_digest_burst_threshold'"
        ).fetchone()
        burst_threshold = max(0, int(burst_row["value"])) if burst_row else 0
    except Exception:
        burst_threshold = 0

    # Offene Funde gruppieren: file_owner ↔ persons (user_id | ad_name).
    #
    # Hash-Dedup: Eine inhaltlich identische Datei (gleicher ``file_hash``)
    # gilt fachlich als bereits bekannt, sobald *irgendeine* Kopie davon an
    # einem IDV hängt – egal, ob über ``idv_register.file_id`` oder
    # ``idv_file_links``. In dem Fall darf für die neu aufgetauchte Kopie
    # keine Owner-Sammelmail mehr ausgehen, sonst bekommt der Fachbereich
    # für jede Umbenennung / jeden Kopie-Vorgang eine separate
    # Benachrichtigung über dieselbe Datei.
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
              OR LOWER(p.ad_name) = LOWER(f.file_owner)
               )
         WHERE f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND f.file_owner IS NOT NULL AND f.file_owner <> ''
           AND NOT EXISTS (SELECT 1 FROM idv_register r   WHERE r.file_id = f.id)
           AND NOT EXISTS (SELECT 1 FROM idv_file_links l WHERE l.file_id = f.id)
           AND NOT EXISTS (
                 SELECT 1
                   FROM idv_files f2
                  WHERE f2.file_hash = f.file_hash
                    AND f2.id <> f.id
                    AND (
                         EXISTS (SELECT 1 FROM idv_register   r2 WHERE r2.file_id = f2.id)
                      OR EXISTS (SELECT 1 FROM idv_file_links l2 WHERE l2.file_id = f2.id)
                    )
               )
    """).fetchall()

    if not rows:
        return {"sent": 0, "candidates": [], "skipped_test_limit": 0}

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
    if not base_url or (not test_mode and not secret_key):
        # Im Testmodus reicht base_url; ein fehlender SECRET_KEY ist nur
        # für die echte Token-Erzeugung kritisch.
        log.warning(
            "Sammelbenachrichtigung übersprungen: app_base_url oder SECRET_KEY fehlt."
        )
        return {"sent": 0, "candidates": [], "skipped_test_limit": 0}

    sent = 0
    candidates: list[dict] = []
    skipped_test_limit = 0
    processed_in_test = 0
    for person_id, group in grouped.items():
        file_count = len(group["files"])

        if test_mode:
            # Im Testmodus alle Dedup-Gates ignorieren, aber pro Klick nur
            # eine begrenzte Anzahl Mails an die Test-Adresse senden.
            burst_mode = False
            if processed_in_test >= max(0, test_limit):
                skipped_test_limit += 1
                candidates.append({
                    "email":      group["email"],
                    "name":       group["anzeigename"],
                    "file_count": file_count,
                    "burst":      False,
                    "skipped":    True,
                })
                continue
            processed_in_test += 1
        else:
            # Hartes Tageslimit: pro Empfänger höchstens **eine** Sammel-Mail
            # pro Tag – verhindert Mail-Flut, auch wenn Sofort-Schwelle und
            # Intervall-Ablauf am selben Tag zusammentreffen.
            sent_today = db.execute(
                "SELECT 1 FROM notification_log "
                "WHERE kind=? AND ref_id=? AND sent_date = date('now')",
                (_OWNER_DIGEST_KIND, person_id),
            ).fetchone()
            if sent_today and not force:
                continue

            # Dedup pro Empfänger + Intervall. Sofort-Versand: wenn die Anzahl
            # offener Funde die Schwelle erreicht, wird das Intervall ignoriert.
            recent = db.execute(
                "SELECT 1 FROM notification_log "
                "WHERE kind=? AND ref_id=? AND sent_date >= date('now', ?)",
                (_OWNER_DIGEST_KIND, person_id, f"-{freq_days} days"),
            ).fetchone()
            burst_mode = bool(
                recent and burst_threshold > 0 and file_count >= burst_threshold
            )
            if recent and not burst_mode and not force:
                continue

        if test_mode:
            # Kein gültiges Token erzeugen/registrieren – der Link in der
            # Test-Mail ist nur für die Layout-Vorschau gedacht.
            magic_link = f"{base_url}/selbst/meine-funde?token=TEST-MODE-NO-VALID-TOKEN"
            jti = None
        else:
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

        recipient_email = test_recipient if test_mode else group["email"]
        test_banner = None
        if test_mode:
            test_banner = (
                f"Eigentlicher Empfänger: {group['anzeigename']} "
                f"&lt;{group['email']}&gt; — Magic-Link in dieser Mail ist "
                "ein Platzhalter und nicht funktionsfähig."
            )

        try:
            ok = notify_owner_digest(
                db,
                recipient_email=recipient_email,
                recipient_name=group["anzeigename"],
                file_rows=group["files"],
                magic_link=magic_link,
                base_url=base_url,
                burst=burst_mode,
                test_banner=test_banner,
            )
        except Exception:
            log.exception(
                "Fehler beim Versand der Sammelbenachrichtigung "
                "(person_id=%s, burst=%s, test=%s)",
                person_id, burst_mode, test_mode,
            )
            ok = False

        candidates.append({
            "email":      group["email"],
            "name":       group["anzeigename"],
            "file_count": file_count,
            "burst":      burst_mode,
            "skipped":    False,
            "sent":       ok,
        })

        if ok:
            if not test_mode:
                _record_sent(_OWNER_DIGEST_KIND, person_id, today_iso)
            sent += 1
        elif jti is not None:
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

    return {
        "sent": sent,
        "candidates": candidates,
        "skipped_test_limit": skipped_test_limit,
    }


def _dispatch_idv_incomplete_reminders(db, today_iso: str) -> int:
    """Mailt Erinnerung an die Fachverantwortlichen für IDVs, deren
    Vollständigkeits-Score < 100 % beträgt (Issue #348).

    Regeln:
    - Kandidaten-Quelle ist ``v_unvollstaendige_idvs`` — dieselbe
      Definition, die das Dashboard für den Zähler nutzt.
    - Dedup-Fenster: 7 Tage (``_DEDUP_WINDOW_DAYS``).
    - Harte Obergrenze: höchstens ``_IDV_INCOMPLETE_MAX_REMINDERS``
      Wiederholungen pro IDV (über ``notification_log``-Einträge gezählt).
    - Archivierte IDVs werden von der View bereits gefiltert.
    - Mail geht an den Fachverantwortlichen (bzw. dessen aktiven
      Stellvertreter via ``_effective_email``).
    """
    from .email_service import (
        notify_idv_incomplete,
        get_configured_recipient_roles,
    )

    active_roles = set(get_configured_recipient_roles(db, "idv_incomplete_reminder"))
    if not active_roles:
        return 0

    # Vollständigkeits-Score lokal berechnen, um keine zweite DB-Runde
    # pro IDV zu brauchen. Nutzt dieselbe Funktion wie die Detailansicht.
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))))
    from db import idv_completeness_score

    rows = db.execute("""
        SELECT r.id                       AS idv_db_id,
               r.idv_id                   AS idv_id,
               r.bezeichnung              AS bezeichnung,
               r.idv_entwickler_id        AS idv_entwickler_id,
               p.id                       AS person_id,
               p.email                    AS email
          FROM v_unvollstaendige_idvs v
          JOIN idv_register r ON r.idv_id = v.idv_id
          JOIN persons p      ON p.id     = r.fachverantwortlicher_id
         WHERE p.aktiv = 1
           AND p.email IS NOT NULL AND p.email <> ''
    """).fetchall()

    cc_emails: set[str] = set()
    if "idv_administrator" in active_roles or "idv_koordinator" in active_roles:
        wanted = []
        if "idv_administrator" in active_roles:
            wanted.append("IDV-Administrator")
        if "idv_koordinator" in active_roles:
            wanted.append("IDV-Koordinator")
        placeholders = ",".join("?" * len(wanted))
        for cc in db.execute(
            f"SELECT email FROM persons WHERE aktiv=1 "
            f"AND email IS NOT NULL AND email <> '' AND rolle IN ({placeholders})",
            wanted,
        ).fetchall():
            cc_emails.add(cc["email"])

    sent = 0
    for r in rows:
        if _recent_sent(db, _IDV_INCOMPLETE_KIND, r["idv_db_id"]):
            continue

        total_sent = db.execute(
            "SELECT COUNT(*) AS c FROM notification_log "
            "WHERE kind=? AND ref_id=?",
            (_IDV_INCOMPLETE_KIND, r["idv_db_id"]),
        ).fetchone()
        if total_sent and total_sent["c"] >= _IDV_INCOMPLETE_MAX_REMINDERS:
            # Obergrenze erreicht — künftig keine Mails mehr für diese IDV.
            continue

        score_info = idv_completeness_score(db, r["idv_db_id"])
        emails: set[str] = set(cc_emails)
        if "fachverantwortlicher" in active_roles:
            emails.add(_effective_email(db, r["person_id"], r["email"]))
        if "idv_entwickler" in active_roles and r["idv_entwickler_id"]:
            ent = db.execute(
                "SELECT id, email FROM persons WHERE id=? AND aktiv=1 "
                "AND email IS NOT NULL AND email <> ''",
                (r["idv_entwickler_id"],),
            ).fetchone()
            if ent:
                emails.add(_effective_email(db, ent["id"], ent["email"]))
        emails = {e for e in emails if e and "@" in e}
        if not emails:
            continue
        try:
            ok = notify_idv_incomplete(
                db, r, score_info["score"], score_info["missing"], sorted(emails),
            )
        except Exception:
            log.exception(
                "Fehler beim Versand der IDV-Nachpflege-Erinnerung (idv_db_id=%s)",
                r["idv_db_id"],
            )
            ok = False
        if ok:
            _record_sent(_IDV_INCOMPLETE_KIND, r["idv_db_id"], today_iso)
            sent += 1
    return sent


def _last_owner_action_date(db, person_id: int):
    """Liefert das ISO-Datum (YYYY-MM-DD) der juengsten Self-Service-Aktion
    des Empfaengers — oder None, wenn nie reagiert wurde."""
    try:
        row = db.execute(
            "SELECT MAX(created_at) AS m FROM self_service_audit "
            "WHERE person_id = ?",
            (person_id,),
        ).fetchone()
        if row and row["m"]:
            return str(row["m"])[:10]
    except Exception:
        pass
    return None


def _has_open_funde(db, person_id: int) -> bool:
    """True, wenn fuer den Empfaenger noch offene Self-Service-Funde existieren."""
    try:
        row = db.execute("""
            SELECT 1 FROM idv_files f
              JOIN persons p ON p.aktiv = 1
                 AND (p.user_id = f.file_owner OR p.ad_name = f.file_owner)
             WHERE p.id = ?
               AND f.status='active'
               AND f.bearbeitungsstatus='Neu'
             LIMIT 1
        """, (person_id,)).fetchone()
        return row is not None
    except Exception:
        return False


def _dispatch_self_service_escalations(db, today_iso: str) -> int:
    """Dreistufige Eskalation fuer ungenutzte Self-Service-Links (Issue #355).

    Stufe 1 (Default 7 Tage):  Reminder an den Owner persoenlich
    Stufe 2 (Default 14 Tage): Mail an OE-Leiter (``persons.oe_leiter_id``)
    Stufe 3 (Default 21 Tage): Eintrag im Ausnahmen-Dashboard des Koordinators

    „Reaktion" = beliebige Self-Service-Aktion in ``self_service_audit``
    nach Versand des Magic-Links. Bei Owner-Aktion innerhalb der Frist
    wird die Eskalations-Kette implizit zurueckgesetzt: weitere Tokens
    starten ihre Frist-Zaehlung erneut.
    """
    cfg = _load_notification_settings(db)
    try:
        d_remind  = int(cfg.get("escalation_reminder_days", "7") or "7")
        d_lead    = int(cfg.get("escalation_to_lead_days", "14") or "14")
        d_coord   = int(cfg.get("escalation_to_coordinator_days", "21") or "21")
    except (TypeError, ValueError):
        d_remind, d_lead, d_coord = 7, 14, 21

    rows = db.execute("""
        SELECT t.person_id, MIN(t.created_at) AS first_token,
               p.email, (p.vorname || ' ' || p.nachname) AS name,
               p.oe_leiter_id,
               (lp.vorname || ' ' || lp.nachname) AS lead_name,
               lp.email AS lead_email
          FROM self_service_tokens t
          JOIN persons p  ON p.id  = t.person_id  AND p.aktiv = 1
          LEFT JOIN persons lp ON lp.id = p.oe_leiter_id AND lp.aktiv = 1
         WHERE t.first_used_at IS NULL
           AND t.revoked_at    IS NULL
         GROUP BY t.person_id
    """).fetchall()

    sent = 0
    from datetime import date as _date_, datetime as _dt_

    def _days_since(iso_str: str) -> int:
        try:
            d = _dt_.fromisoformat(iso_str.replace(" ", "T")).date()
        except Exception:
            try:
                d = _date_.fromisoformat(iso_str[:10])
            except Exception:
                return -1
        return (_date_.today() - d).days

    for r in rows:
        if not _has_open_funde(db, int(r["person_id"])):
            continue
        # Reset-Logik: wenn der Owner nach Token-Versand reagiert hat, gilt
        # die Eskalations-Kette als abgebrochen.
        last_action = _last_owner_action_date(db, int(r["person_id"]))
        if last_action and last_action >= str(r["first_token"])[:10]:
            continue

        days = _days_since(str(r["first_token"]))
        if days < d_remind:
            continue

        # Stufe 1
        if d_remind <= days < d_lead:
            if not _recent_sent(db, _ESC_REMIND_OWNER, int(r["person_id"])):
                ok = False
                try:
                    from .email_service import notify_self_service_escalation
                    ok = notify_self_service_escalation(
                        db, recipient_email=r["email"], recipient_name=r["name"],
                        stage="reminder", days=days,
                    )
                except Exception:
                    log.exception("Eskalation Stufe 1 fehlgeschlagen (person_id=%s)",
                                  r["person_id"])
                if ok:
                    _record_sent(_ESC_REMIND_OWNER, int(r["person_id"]), today_iso)
                    sent += 1
        # Stufe 2
        elif d_lead <= days < d_coord:
            if r["lead_email"] and not _recent_sent(
                    db, _ESC_TO_OE_LEAD, int(r["person_id"])):
                ok = False
                try:
                    from .email_service import notify_self_service_escalation
                    ok = notify_self_service_escalation(
                        db, recipient_email=r["lead_email"],
                        recipient_name=r["lead_name"] or "OE-Leitung",
                        stage="oe_lead", days=days,
                        owner_name=r["name"], owner_email=r["email"],
                    )
                except Exception:
                    log.exception("Eskalation Stufe 2 fehlgeschlagen (person_id=%s)",
                                  r["person_id"])
                if ok:
                    _record_sent(_ESC_TO_OE_LEAD, int(r["person_id"]), today_iso)
                    sent += 1
        # Stufe 3
        elif days >= d_coord:
            if not _recent_sent(db, _ESC_TO_COORDINATOR, int(r["person_id"])):
                # Stufe 3 wird nicht als Mail versandt, sondern via
                # Audit-Eintrag fuer das Ausnahmen-Dashboard markiert
                # (vgl. Issue #353).
                _record_sent(_ESC_TO_COORDINATOR, int(r["person_id"]), today_iso)
                sent += 1
    return sent


def _run_daily_dispatch(app) -> None:
    """Einmaliger Durchlauf für den aktuellen Tag (idempotent dank Dedup)."""
    with app.app_context():
        db = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        m_sent = _dispatch_overdue_measures(db, today)
        r_sent = _dispatch_due_reviews(db, today)
        p_sent = _dispatch_pool_claim_reminders(db, today)
        o_sent = _dispatch_owner_digest(db, today)["sent"]
        i_sent = _dispatch_idv_incomplete_reminders(db, today)
        e_sent = _dispatch_self_service_escalations(db, today)
        log.info(
            "Notification-Dispatch abgeschlossen: %d Maßnahmen, %d Prüfungen, "
            "%d Pool-Reminder, %d Sammelbenachrichtigung(en) an Owner, "
            "%d IDV-Nachpflege-Erinnerung(en), %d Self-Service-Eskalation(en).",
            m_sent, r_sent, p_sent, o_sent, i_sent, e_sent,
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
        name="idvscope-notification-scheduler",
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
            "massnahmen":             _dispatch_overdue_measures(db, today),
            "pruefungen":             _dispatch_due_reviews(db, today),
            "pool_reminder":          _dispatch_pool_claim_reminders(db, today),
            "owner_digest":           _dispatch_owner_digest(db, today)["sent"],
            "idv_incomplete_reminder": _dispatch_idv_incomplete_reminders(db, today),
            "self_service_escalations": _dispatch_self_service_escalations(db, today),
        }
