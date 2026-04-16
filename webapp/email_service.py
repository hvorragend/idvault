"""
idvault – E-Mail-Benachrichtigungs-Service
==========================================
Sendet transaktionale E-Mails via SMTP (TLS/STARTTLS).
Konfiguration wird aus der app_settings-Tabelle gelesen.

Alle E-Mail-Vorlagen (Betreff + HTML-Body) sind über die Admin-Oberfläche
konfigurierbar. Platzhalter werden im Format {name} ersetzt.

Umgebungsvariablen (überschreiben DB-Einstellungen):
    IDV_SMTP_HOST, IDV_SMTP_PORT, IDV_SMTP_USER,
    IDV_SMTP_PASSWORD, IDV_SMTP_FROM, IDV_SMTP_TLS
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from typing import Optional

log = logging.getLogger("idvault.email")


# ---------------------------------------------------------------------------
# SMTP-Passwort-Speicherung (VULN-007 Remediation)
# ---------------------------------------------------------------------------
#
# Das SMTP-Passwort wurde bislang als Klartext in app_settings.smtp_password
# abgelegt. Es wird nun analog zum LDAP-Bind-Passwort mit Fernet (AES-128-CBC
# + HMAC-SHA256) verschlüsselt gespeichert. Verschlüsselte Werte tragen das
# Präfix "enc:", damit Altbestände (Klartext) automatisch erkannt und beim
# nächsten Speichervorgang migriert werden.
#
# Der Fernet-Schlüssel wird – wie bei ldap_auth – aus SECRET_KEY abgeleitet.
# Bei einer SECRET_KEY-Rotation muss das SMTP-Passwort neu gesetzt werden.

_ENC_PREFIX = "enc:"


def _smtp_fernet():
    """Lokale Fernet-Instanz auf Basis des aktuellen SECRET_KEY.

    Importiert lazy, damit das E-Mail-Modul auch ohne Anwendungskontext
    (z. B. in Tests) importierbar bleibt.
    """
    from flask import current_app
    from .ldap_auth import _fernet  # gleiche Ableitung wie LDAP-Bind-Passwort
    secret_key = current_app.config.get("SECRET_KEY", "")
    return _fernet(secret_key)


def encrypt_smtp_password(plain: str) -> str:
    """Verschlüsselt ein SMTP-Klartextpasswort (mit "enc:"-Präfix)."""
    if not plain:
        return ""
    token = _smtp_fernet().encrypt(plain.encode()).decode()
    return _ENC_PREFIX + token


def _decrypt_smtp_password(stored: str) -> str:
    """Entschlüsselt einen gespeicherten SMTP-Passwortwert.

    Erkennt sowohl verschlüsselte Werte (mit "enc:"-Präfix) als auch
    Altbestände im Klartext und gibt den Klartext zurück.
    """
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        # Legacy-Klartext – wird beim nächsten Speichern automatisch migriert.
        return stored
    try:
        token = stored[len(_ENC_PREFIX):]
        return _smtp_fernet().decrypt(token.encode()).decode()
    except Exception as exc:
        log.warning("SMTP-Passwort kann nicht entschlüsselt werden: %s", exc)
        return ""


def _get_smtp_config(db) -> dict:
    """Liest SMTP-Einstellungen aus DB, mit Env-Überschreibung."""
    try:
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
        cfg  = {r["key"]: r["value"] for r in rows}
    except Exception:
        cfg  = {}

    # Passwort: Umgebungsvariable hat Vorrang (Klartext); DB-Wert wird ggf. entschlüsselt
    env_pw = os.environ.get("IDV_SMTP_PASSWORD")
    if env_pw is not None:
        password = env_pw
    else:
        password = _decrypt_smtp_password(cfg.get("smtp_password", ""))

    return {
        "host":     os.environ.get("IDV_SMTP_HOST",     cfg.get("smtp_host",     "")),
        "port":     int(os.environ.get("IDV_SMTP_PORT", cfg.get("smtp_port",     587))),
        "user":     os.environ.get("IDV_SMTP_USER",     cfg.get("smtp_user",     "")),
        "password": password,
        "from":     os.environ.get("IDV_SMTP_FROM",     cfg.get("smtp_from",     "")),
        "tls":      os.environ.get("IDV_SMTP_TLS",      cfg.get("smtp_tls",      "1")) == "1",
    }


def _smtp_log(db, recipients: list, subject: str, success: bool,
              error_msg: str = "") -> None:
    """Schreibt einen Eintrag in smtp_log und begrenzt die Tabelle auf 200 Zeilen."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        db.execute(
            "INSERT INTO smtp_log (sent_at, recipients, subject, success, error_msg) "
            "VALUES (?,?,?,?,?)",
            (now, ", ".join(recipients), subject, 1 if success else 0, error_msg or ""),
        )
        # Älteste Einträge löschen, damit die Tabelle überschaubar bleibt
        db.execute(
            "DELETE FROM smtp_log WHERE id NOT IN "
            "(SELECT id FROM smtp_log ORDER BY id DESC LIMIT 200)"
        )
        db.commit()
    except Exception as exc:
        log.warning("smtp_log konnte nicht geschrieben werden: %s", exc)


def send_mail(db, to: str | list[str], subject: str,
              body_html: str, body_text: str = "") -> bool:
    """Sendet eine E-Mail. Gibt True bei Erfolg zurück."""
    cfg = _get_smtp_config(db)
    if not cfg["host"] or not cfg["from"]:
        log.warning("E-Mail nicht konfiguriert (smtp_host / smtp_from fehlen).")
        return False

    recipients = [to] if isinstance(to, str) else to
    recipients = [r for r in recipients if r and "@" in r]
    if not recipients:
        log.warning("Keine gültigen Empfänger-Adressen.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("idvault", cfg["from"]))
    msg["To"]      = ", ".join(recipients)

    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        if cfg["tls"]:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)

        if cfg["user"] and cfg["password"]:
            smtp.login(cfg["user"], cfg["password"])

        smtp.sendmail(cfg["from"], recipients, msg.as_string())
        smtp.quit()
        log.info("E-Mail gesendet an %s: %s", recipients, subject)
        _smtp_log(db, recipients, subject, True)
        return True
    except Exception as exc:
        err = str(exc)
        log.error("E-Mail-Fehler: %s", err)
        _smtp_log(db, recipients, subject, False, err)
        return False


def send_smtp_test(db, to_email: str) -> tuple[bool, str]:
    """Sendet eine Test-E-Mail und gibt (Erfolg, Meldung) zurück.

    Kann auch ohne konfigurierte Vorlagen aufgerufen werden.
    Gibt bei fehlender Konfiguration sofort einen Fehler zurück,
    ohne einen SMTP-Verbindungsversuch zu starten.
    """
    cfg = _get_smtp_config(db)
    if not cfg["host"]:
        return False, "SMTP-Host ist nicht konfiguriert."
    if not cfg["from"]:
        return False, "Absenderadresse (smtp_from) ist nicht konfiguriert."
    if not to_email or "@" not in to_email:
        return False, "Ungültige Empfänger-Adresse."

    subject = "[idvault] SMTP-Verbindungstest"
    body_html = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#198754;">idvault – SMTP-Test erfolgreich</h2>
<p>Diese E-Mail wurde automatisch als Verbindungstest gesendet.</p>
<p>Die SMTP-Konfiguration funktioniert korrekt.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

    recipients = [to_email]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("idvault", cfg["from"]))
    msg["To"]      = to_email
    msg.attach(MIMEText("idvault – SMTP-Test erfolgreich.\n\nDiese E-Mail wurde automatisch als Verbindungstest gesendet.", "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        if cfg["tls"]:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)

        if cfg["user"] and cfg["password"]:
            smtp.login(cfg["user"], cfg["password"])

        smtp.sendmail(cfg["from"], recipients, msg.as_string())
        smtp.quit()
        log.info("SMTP-Test-E-Mail gesendet an %s", to_email)
        _smtp_log(db, recipients, subject, True)
        return True, f"Test-E-Mail erfolgreich gesendet an {to_email}."
    except Exception as exc:
        err = str(exc)
        log.error("SMTP-Test-Fehler: %s", err)
        _smtp_log(db, recipients, subject, False, err)
        return False, f"Fehler: {err}"


# ---------------------------------------------------------------------------
# Zentrale Template-Mechanik
# ---------------------------------------------------------------------------

def _replace_placeholders(template: str, placeholders: dict) -> str:
    """Ersetzt {name}-Platzhalter im Template."""
    for key, val in placeholders.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def _load_template(db, tpl_key: str, default_subject: str, default_body: str,
                   placeholders: dict) -> tuple[str, str]:
    """Lädt Betreff und HTML-Body für einen Template-Key aus app_settings.

    Gibt (subject, html_body) zurück mit ersetzten Platzhaltern.
    Fällt auf die übergebenen Defaults zurück, wenn kein DB-Eintrag existiert.
    """
    subject_key = f"email_tpl_{tpl_key}_subject"
    body_key    = f"email_tpl_{tpl_key}_body"

    try:
        row_s = db.execute(
            "SELECT value FROM app_settings WHERE key=?", (subject_key,)
        ).fetchone()
        row_b = db.execute(
            "SELECT value FROM app_settings WHERE key=?", (body_key,)
        ).fetchone()
    except Exception:
        row_s = row_b = None

    subject = (row_s["value"] if row_s and row_s["value"] else default_subject)
    body    = (row_b["value"] if row_b and row_b["value"] else default_body)

    return (
        _replace_placeholders(subject, placeholders),
        _replace_placeholders(body, placeholders),
    )


def _strip_html_tags(html: str) -> str:
    """Einfache HTML→Text-Konvertierung für den Fallback-Plaintext."""
    import re
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'</?(p|div|h[1-6]|li|tr|td|th)[^>]*>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Default-Templates (Fallback, wenn nichts in app_settings konfiguriert)
# ---------------------------------------------------------------------------

_DEFAULTS = {}

# ── 1. Neue Datei erkannt ─────────────────────────────────────────────────

_DEFAULTS["neue_datei_subject"] = "[idvault] Neue IDV-Datei erkannt: {dateiname}"
_DEFAULTS["neue_datei_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#0d6efd;">idvault – Neue Datei erkannt</h2>
<p>Der idvault-Scanner hat eine neue Datei entdeckt, die noch nicht im IDV-Register
   erfasst ist:</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">Dateiname</td>
      <td style="padding:6px;">{dateiname}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Pfad</td>
      <td style="padding:6px;font-family:monospace;font-size:12px;">{pfad}</td></tr>
  <tr><td style="padding:6px;font-weight:bold;">Erstmals erkannt</td>
      <td style="padding:6px;">{erkannt_am}</td></tr>
</table>
<p style="margin-top:20px;">Bitte melden Sie sich in idvault an und erfassen
   Sie die Datei im IDV-Register.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

# ── 2. Prüfung fällig ────────────────────────────────────────────────────

_DEFAULTS["pruefung_faellig_subject"] = "[idvault] Prüfung fällig: {idv_id} – {bezeichnung}"
_DEFAULTS["pruefung_faellig_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#fd7e14;">idvault – Prüfung fällig</h2>
<p>Die Prüfung für folgendes IDV ist fällig oder überfällig:</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">IDV-ID</td>
      <td style="padding:6px;">{idv_id}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Bezeichnung</td>
      <td style="padding:6px;">{bezeichnung}</td></tr>
  <tr><td style="padding:6px;font-weight:bold;">Fällig am</td>
      <td style="padding:6px;">{faellig_am}</td></tr>
</table>
<p style="margin-top:16px;">Bitte melden Sie sich in idvault an und führen
   Sie die Prüfung durch.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

# ── 3. Freigabe-Schritt offen ────────────────────────────────────────────

_DEFAULTS["freigabe_schritt_subject"] = "[idvault] Freigabe-Schritt offen: {schritt} – {idv_id}"
_DEFAULTS["freigabe_schritt_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#0d6efd;">idvault – Test &amp; Freigabe</h2>
<p>Für die folgende IDV steht ein Freigabe-Schritt zur Bearbeitung bereit:</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">IDV-ID</td>
      <td style="padding:6px;">{idv_id}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Bezeichnung</td>
      <td style="padding:6px;">{bezeichnung}</td></tr>
  <tr><td style="padding:6px;font-weight:bold;">Schritt</td>
      <td style="padding:6px;font-weight:bold;color:#0d6efd;">{schritt}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Versionskommentar</td>
      <td style="padding:6px;font-style:italic;">{versionskommentar}</td></tr>
</table>
<p style="margin-top:16px;color:#6c757d;font-size:12px;">
  Bitte melden Sie sich in idvault an und schließen Sie den Schritt ab.<br>
  Hinweis: Gemäß Funktionstrennung darf der Entwickler der IDV keine
  Freigabe-Schritte abschließen.</p>
<p style="color:#6c757d;font-size:12px;margin-top:16px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

# ── 4. IDV freigegeben (alle 4 Schritte erledigt) ────────────────────────

_DEFAULTS["freigabe_abgeschlossen_subject"] = "[idvault] IDV freigegeben: {idv_id} – {bezeichnung}"
_DEFAULTS["freigabe_abgeschlossen_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#198754;">idvault – IDV freigegeben</h2>
<p>Alle vier Freigabe-Schritte wurden erfolgreich abgeschlossen:</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">IDV-ID</td>
      <td style="padding:6px;">{idv_id}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Bezeichnung</td>
      <td style="padding:6px;">{bezeichnung}</td></tr>
</table>
<ul style="margin-top:12px;">
  <li>Fachlicher Test – erledigt</li>
  <li>Technischer Test – erledigt</li>
  <li>Fachliche Abnahme – erledigt</li>
  <li>Technische Abnahme – erledigt</li>
</ul>
<p>Die IDV wurde auf Status <strong>Freigegeben</strong> und Dokumentationsstatus
   <strong>Dokumentiert</strong> gesetzt.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

# ── 5. Bewertungsanforderung (an Datei-Ersteller) ────────────────────────

_DEFAULTS["bewertung_subject"] = "[idvault] Bitte um Bewertung: {dateiname}"
_DEFAULTS["bewertung_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#0d6efd;">idvault – Bewertung angefordert</h2>
<p>Sehr geehrte/r {ersteller},</p>
<p>die folgende Datei wurde vom idvault-Scanner erkannt und ist Ihnen als
   Ersteller/Eigentümer zugeordnet. Bitte bewerten Sie, ob diese Datei als
   <strong>Individuelle Datenverarbeitung (IDV)</strong> im Sinne von MaRisk AT 7.2
   einzustufen ist.</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">Dateiname</td>
      <td style="padding:6px;">{dateiname}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Pfad</td>
      <td style="padding:6px;font-family:monospace;font-size:12px;">{pfad}</td></tr>
  <tr><td style="padding:6px;font-weight:bold;">Formeln</td>
      <td style="padding:6px;">{formelanzahl}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Makros</td>
      <td style="padding:6px;">{makros}</td></tr>
</table>
<p style="margin-top:20px;">Bitte melden Sie sich in idvault an und nehmen
   Sie die Bewertung vor.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

# ── 6. Überfällige Maßnahme ──────────────────────────────────────────────

_DEFAULTS["massnahme_ueberfaellig_subject"] = "[idvault] Überfällige Maßnahme: {titel}"
_DEFAULTS["massnahme_ueberfaellig_body"] = """\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#dc3545;">idvault – Überfällige Maßnahme</h2>
<p>Die folgende Maßnahme ist überfällig:</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px;font-weight:bold;width:160px;">Titel</td>
      <td style="padding:6px;">{titel}</td></tr>
  <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Fällig am</td>
      <td style="padding:6px;">{faellig_am}</td></tr>
</table>
<p style="margin-top:16px;">Bitte melden Sie sich in idvault an und bearbeiten
   Sie die Maßnahme.</p>
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Template-Registry (für Admin-UI und Validierung)
# ---------------------------------------------------------------------------

EMAIL_TEMPLATES = {
    "neue_datei": {
        "label": "Neue Datei erkannt",
        "placeholders": ["dateiname", "pfad", "erkannt_am"],
    },
    "pruefung_faellig": {
        "label": "Prüfung fällig",
        "placeholders": ["idv_id", "bezeichnung", "faellig_am"],
    },
    "freigabe_schritt": {
        "label": "Freigabe-Schritt offen",
        "placeholders": ["idv_id", "bezeichnung", "schritt", "versionskommentar"],
    },
    "freigabe_abgeschlossen": {
        "label": "IDV freigegeben (alle Schritte erledigt)",
        "placeholders": ["idv_id", "bezeichnung"],
    },
    "bewertung": {
        "label": "Bewertungsanforderung an Datei-Ersteller",
        "placeholders": ["dateiname", "pfad", "ersteller", "formelanzahl", "makros"],
    },
    "massnahme_ueberfaellig": {
        "label": "Überfällige Maßnahme",
        "placeholders": ["titel", "faellig_am"],
    },
}


# ---------------------------------------------------------------------------
# Benachrichtigungs-Funktionen
# ---------------------------------------------------------------------------

def _is_notify_enabled(db, tpl_key: str) -> bool:
    """Prüft ob der Mailversand für diesen Template-Typ in den Einstellungen aktiviert ist.

    Liest den Key ``notify_enabled_{tpl_key}`` aus app_settings.
    Fehlt der Eintrag, gilt die Benachrichtigung als aktiviert (sicherer Default).
    Rückwärtskompatibilität: Für 'neue_datei' wird zusätzlich der alte
    Key 'notify_new_file' als Fallback geprüft.
    """
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=?",
            (f"notify_enabled_{tpl_key}",)
        ).fetchone()
        if row is not None:
            return row["value"] == "1"
        # Rückwärtskompatibilität für den alten 'notify_new_file'-Key
        if tpl_key == "neue_datei":
            row = db.execute(
                "SELECT value FROM app_settings WHERE key='notify_new_file'"
            ).fetchone()
            return bool(row and row["value"] == "1")
        return True  # Kein Eintrag → Default aktiviert
    except Exception:
        return True


def notify_new_scanner_file(db, file_row, responsible_emails: list[str]) -> bool:
    """Benachrichtigt Verantwortliche über eine neu erkannte Datei."""
    if not _is_notify_enabled(db, "neue_datei"):
        return False

    fname    = file_row["file_name"] if hasattr(file_row, "__getitem__") else str(file_row)
    fpath    = file_row["full_path"] if hasattr(file_row, "__getitem__") else ""
    detected = file_row["first_seen_at"] if hasattr(file_row, "__getitem__") else ""

    placeholders = {
        "dateiname":  fname,
        "pfad":       fpath,
        "erkannt_am": detected[:10] if detected else "–",
    }

    subject, html = _load_template(
        db, "neue_datei",
        _DEFAULTS["neue_datei_subject"],
        _DEFAULTS["neue_datei_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, responsible_emails, subject, html, text)


def notify_review_due(db, idv_row, responsible_email: str) -> bool:
    """Erinnerung an fällige Prüfung."""
    if not _is_notify_enabled(db, "pruefung_faellig"):
        return False
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""
    datum  = idv_row["naechste_pruefung"] if hasattr(idv_row, "__getitem__") else ""

    placeholders = {
        "idv_id":      idv_id,
        "bezeichnung": name,
        "faellig_am":  datum[:10] if datum else "–",
    }

    subject, html = _load_template(
        db, "pruefung_faellig",
        _DEFAULTS["pruefung_faellig_subject"],
        _DEFAULTS["pruefung_faellig_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, responsible_email, subject, html, text)


def notify_freigabe_schritt(db, idv_row, schritt: str,
                            recipient_emails: list,
                            versions_kommentar: str = None) -> bool:
    """Benachrichtigt Prüfer über einen neuen offenen Freigabe-Schritt."""
    if not _is_notify_enabled(db, "freigabe_schritt"):
        return False
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""

    placeholders = {
        "idv_id":             idv_id,
        "bezeichnung":        name,
        "schritt":            schritt,
        "versionskommentar":  versions_kommentar or "–",
    }

    subject, html = _load_template(
        db, "freigabe_schritt",
        _DEFAULTS["freigabe_schritt_subject"],
        _DEFAULTS["freigabe_schritt_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, recipient_emails, subject, html, text)


def notify_freigabe_abgeschlossen(db, idv_row, recipient_emails: list) -> bool:
    """Benachrichtigung wenn alle 4 Freigabe-Schritte erledigt wurden."""
    if not _is_notify_enabled(db, "freigabe_abgeschlossen"):
        return False
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""

    placeholders = {
        "idv_id":      idv_id,
        "bezeichnung": name,
    }

    subject, html = _load_template(
        db, "freigabe_abgeschlossen",
        _DEFAULTS["freigabe_abgeschlossen_subject"],
        _DEFAULTS["freigabe_abgeschlossen_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, recipient_emails, subject, html, text)


def get_app_base_url(db) -> str:
    """Liest die App-Basis-URL aus DB oder Umgebungsvariable."""
    env_url = os.environ.get("IDV_APP_BASE_URL", "")
    if env_url:
        return env_url.rstrip("/")
    try:
        row = db.execute("SELECT value FROM app_settings WHERE key='app_base_url'").fetchone()
        if row and row["value"]:
            return row["value"].rstrip("/")
    except Exception:
        pass
    return ""


def notify_file_bewertung(db, file_row, recipient_email: str) -> bool:
    """Sendet eine Bewertungsanforderung an den Datei-Ersteller/-Eigentümer."""
    if not _is_notify_enabled(db, "bewertung"):
        return False
    fname = file_row["file_name"] if hasattr(file_row, "__getitem__") else str(file_row)
    fpath = file_row["full_path"] if hasattr(file_row, "__getitem__") else ""
    formula_count = file_row["formula_count"] if hasattr(file_row, "__getitem__") else 0
    has_macros = file_row["has_macros"] if hasattr(file_row, "__getitem__") else 0
    ersteller = (file_row.get("file_owner") or file_row.get("office_author") or "–") if hasattr(file_row, "__getitem__") else "–"

    placeholders = {
        "dateiname":    fname,
        "pfad":         fpath,
        "ersteller":    ersteller,
        "formelanzahl": str(formula_count or 0),
        "makros":       "Ja" if has_macros else "Nein",
    }

    subject, html = _load_template(
        db, "bewertung",
        _DEFAULTS["bewertung_subject"],
        _DEFAULTS["bewertung_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_file_bewertung_batch(db, file_rows: list, recipient_email: str,
                                base_url: str = "") -> bool:
    """Sendet eine kombinierte Bewertungsanforderung für mehrere Dateien an einen Empfänger.

    Fasst alle übergebenen Dateien in einer einzigen E-Mail zusammen.
    Wenn base_url angegeben ist, wird für jede Datei ein Link in idvault eingefügt.
    """
    if not file_rows:
        return False
    if not _is_notify_enabled(db, "bewertung"):
        return False

    ersteller = "–"
    for f in file_rows:
        val = (f.get("file_owner") or f.get("office_author") or "") if hasattr(f, "__getitem__") else ""
        if val:
            ersteller = val
            break

    n = len(file_rows)
    if n == 1:
        subject = f"[idvault] Bitte um Bewertung: {file_rows[0]['file_name']}"
    else:
        subject = f"[idvault] Bitte um Bewertung: {n} Dateien"

    with_links = bool(base_url)

    # Tabellen-Header
    link_th = '<th style="padding:8px;text-align:left;">Link</th>' if with_links else ""
    rows_html = ""
    for i, f in enumerate(file_rows):
        bg = ' style="background:#f8f9fa"' if i % 2 == 0 else ""
        fname = f["file_name"] if hasattr(f, "__getitem__") else str(f)
        fpath = f["full_path"] if hasattr(f, "__getitem__") else ""
        formula_count = f["formula_count"] if hasattr(f, "__getitem__") else 0
        has_macros = f["has_macros"] if hasattr(f, "__getitem__") else 0
        file_id = f["id"] if hasattr(f, "__getitem__") else ""

        link_td = ""
        if with_links and file_id:
            link = f"{base_url}/scanner/funde?highlight={file_id}"
            link_td = f'<td style="padding:8px;vertical-align:top;"><a href="{link}" style="font-size:12px;">In idvault öffnen</a></td>'

        rows_html += f"""
        <tr{bg}>
          <td style="padding:8px;font-weight:bold;vertical-align:top;">{fname}</td>
          <td style="padding:8px;font-family:monospace;font-size:11px;vertical-align:top;">{fpath}</td>
          <td style="padding:8px;text-align:center;vertical-align:top;">{formula_count or 0}</td>
          <td style="padding:8px;text-align:center;vertical-align:top;">{'Ja' if has_macros else 'Nein'}</td>
          {link_td}
        </tr>"""

    scanner_link_html = ""
    if with_links:
        scanner_link_html = (
            f'<p style="margin-top:12px;">'
            f'<a href="{base_url}/scanner/funde">Zum Scanner-Eingang in idvault</a>'
            f'</p>'
        )

    html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#0d6efd;">idvault – Bewertung angefordert</h2>
<p>Sehr geehrte/r {ersteller},</p>
<p>die folgenden Dateien wurden vom idvault-Scanner erkannt und sind Ihnen als
   Ersteller/Eigentümer zugeordnet. Bitte bewerten Sie, ob diese Dateien als
   <strong>Individuelle Datenverarbeitung (IDV)</strong> im Sinne von MaRisk AT 7.2
   einzustufen sind.</p>
<table style="border-collapse:collapse;width:100%;border:1px solid #dee2e6;">
  <thead>
    <tr style="background:#0d6efd;color:#fff;">
      <th style="padding:8px;text-align:left;">Dateiname</th>
      <th style="padding:8px;text-align:left;">Pfad</th>
      <th style="padding:8px;text-align:center;">Formeln</th>
      <th style="padding:8px;text-align:center;">Makros</th>
      {link_th}
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>
<p style="margin-top:20px;">Bitte melden Sie sich in idvault an und nehmen
   Sie die Bewertung vor.</p>
{scanner_link_html}
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_bericht_bewertung_batch(db, bericht_rows: list, recipient_email: str,
                                   base_url: str = "") -> bool:
    """Sendet eine Bewertungsanforderung für Cognos-Berichte an einen Empfänger."""
    if not bericht_rows:
        return False
    if not _is_notify_enabled(db, "bewertung"):
        return False

    eigentuemer = "–"
    for b in bericht_rows:
        val = (b.get("eigentuemer") or "") if hasattr(b, "__getitem__") else ""
        if val:
            eigentuemer = val
            break

    n = len(bericht_rows)
    if n == 1:
        subject = f"[idvault] Bitte um Bewertung: {bericht_rows[0]['berichtsname']}"
    else:
        subject = f"[idvault] Bitte um Bewertung: {n} Cognos-Berichte"

    with_links = bool(base_url)
    link_th = '<th style="padding:8px;text-align:left;">Link</th>' if with_links else ""
    rows_html = ""
    for i, b in enumerate(bericht_rows):
        bg = ' style="background:#f8f9fa"' if i % 2 == 0 else ""
        bname   = b["berichtsname"] if hasattr(b, "__getitem__") else str(b)
        suchpfad = b["suchpfad"] if hasattr(b, "__getitem__") else ""
        bericht_id = b["id"] if hasattr(b, "__getitem__") else ""

        link_td = ""
        if with_links and bericht_id:
            link = f"{base_url}/cognos/"
            link_td = f'<td style="padding:8px;vertical-align:top;"><a href="{link}" style="font-size:12px;">In idvault öffnen</a></td>'

        rows_html += f"""
        <tr{bg}>
          <td style="padding:8px;font-weight:bold;vertical-align:top;">{bname}</td>
          <td style="padding:8px;font-family:monospace;font-size:11px;vertical-align:top;">{suchpfad or '–'}</td>
          {link_td}
        </tr>"""

    cognos_link_html = ""
    if with_links:
        cognos_link_html = (
            f'<p style="margin-top:12px;">'
            f'<a href="{base_url}/cognos/">Zu den Cognos-Berichten in idvault</a>'
            f'</p>'
        )

    html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:14px;">
<h2 style="color:#0d6efd;">idvault – Bewertung angefordert</h2>
<p>Sehr geehrte/r {eigentuemer},</p>
<p>die folgenden Cognos-Berichte sind Ihnen als Eigentümer zugeordnet. Bitte bewerten Sie,
   ob diese Berichte als <strong>Individuelle Datenverarbeitung (IDV)</strong> im Sinne
   von MaRisk AT 7.2 einzustufen sind.</p>
<table style="border-collapse:collapse;width:100%;border:1px solid #dee2e6;">
  <thead>
    <tr style="background:#0d6efd;color:#fff;">
      <th style="padding:8px;text-align:left;">Berichtsname</th>
      <th style="padding:8px;text-align:left;">Suchpfad</th>
      {link_th}
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>
<p style="margin-top:20px;">Bitte melden Sie sich in idvault an und nehmen
   Sie die Bewertung vor.</p>
{cognos_link_html}
<p style="color:#6c757d;font-size:12px;margin-top:30px;">
  Diese Nachricht wurde automatisch von idvault gesendet.</p>
</body></html>"""

    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_measure_overdue(db, massnahme_row, responsible_email: str) -> bool:
    """Eskalation für überfällige Maßnahme."""
    if not _is_notify_enabled(db, "massnahme_ueberfaellig"):
        return False
    titel   = massnahme_row["titel"] if hasattr(massnahme_row, "__getitem__") else str(massnahme_row)
    faellig = massnahme_row["faellig_am"] if hasattr(massnahme_row, "__getitem__") else ""

    placeholders = {
        "titel":      titel,
        "faellig_am": faellig[:10] if faellig else "–",
    }

    subject, html = _load_template(
        db, "massnahme_ueberfaellig",
        _DEFAULTS["massnahme_ueberfaellig_subject"],
        _DEFAULTS["massnahme_ueberfaellig_body"],
        placeholders,
    )
    text = _strip_html_tags(html)
    return send_mail(db, responsible_email, subject, html, text)
