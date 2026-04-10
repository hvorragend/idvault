"""
idvault – E-Mail-Benachrichtigungs-Service
==========================================
Sendet transaktionale E-Mails via SMTP (TLS/STARTTLS).
Konfiguration wird aus der app_settings-Tabelle gelesen.

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


def _get_smtp_config(db) -> dict:
    """Liest SMTP-Einstellungen aus DB, mit Env-Überschreibung."""
    try:
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
        cfg  = {r["key"]: r["value"] for r in rows}
    except Exception:
        cfg  = {}

    return {
        "host":     os.environ.get("IDV_SMTP_HOST",     cfg.get("smtp_host",     "")),
        "port":     int(os.environ.get("IDV_SMTP_PORT", cfg.get("smtp_port",     587))),
        "user":     os.environ.get("IDV_SMTP_USER",     cfg.get("smtp_user",     "")),
        "password": os.environ.get("IDV_SMTP_PASSWORD", cfg.get("smtp_password", "")),
        "from":     os.environ.get("IDV_SMTP_FROM",     cfg.get("smtp_from",     "")),
        "tls":      os.environ.get("IDV_SMTP_TLS",      cfg.get("smtp_tls",      "1")) == "1",
    }


def send_mail(db, to: str | list[str], subject: str,
              body_html: str, body_text: str = "") -> bool:
    """Sendet eine E-Mail. Gibt True bei Erfolg zurück.

    Args:
        db:         Datenbankverbindung (für Konfiguration)
        to:         Empfänger-Adresse(n) als String oder Liste
        subject:    Betreff
        body_html:  HTML-Body
        body_text:  Fallback-Textversion (optional)
    """
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
        return True
    except Exception as exc:
        log.error("E-Mail-Fehler: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Benachrichtigungs-Templates
# ---------------------------------------------------------------------------

def notify_new_scanner_file(db, file_row, responsible_emails: list[str]) -> bool:
    """Benachrichtigt Verantwortliche über eine neu erkannte Datei."""
    try:
        notify_enabled = db.execute(
            "SELECT value FROM app_settings WHERE key='notify_new_file'"
        ).fetchone()
        if not notify_enabled or notify_enabled["value"] != "1":
            return False
    except Exception:
        return False

    fname    = file_row["file_name"] if hasattr(file_row, "__getitem__") else str(file_row)
    fpath    = file_row["full_path"] if hasattr(file_row, "__getitem__") else ""
    detected = file_row["first_seen_at"] if hasattr(file_row, "__getitem__") else ""

    subject = f"[idvault] Neue IDV-Datei erkannt: {fname}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;">
    <h2 style="color:#0d6efd;">idvault – Neue Datei erkannt</h2>
    <p>Der idvault-Scanner hat eine neue Datei entdeckt, die noch nicht im IDV-Register
       erfasst ist:</p>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:6px;font-weight:bold;width:160px;">Dateiname</td>
          <td style="padding:6px;">{fname}</td></tr>
      <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Pfad</td>
          <td style="padding:6px;font-family:monospace;font-size:12px;">{fpath}</td></tr>
      <tr><td style="padding:6px;font-weight:bold;">Erstmals erkannt</td>
          <td style="padding:6px;">{detected[:10] if detected else '–'}</td></tr>
    </table>
    <p style="margin-top:20px;">
      <a href="#" style="background:#0d6efd;color:white;padding:8px 16px;
         text-decoration:none;border-radius:4px;">Im IDV-Register erfassen</a>
    </p>
    <p style="color:#6c757d;font-size:12px;margin-top:30px;">
      Diese Nachricht wurde automatisch von idvault gesendet.
    </p>
    </body></html>
    """
    text = (
        f"idvault – Neue Datei erkannt\n\n"
        f"Datei: {fname}\nPfad:  {fpath}\nErkannt: {detected[:10] if detected else '–'}\n\n"
        f"Bitte im IDV-Register erfassen."
    )
    return send_mail(db, responsible_emails, subject, html, text)


def notify_review_due(db, idv_row, responsible_email: str) -> bool:
    """Erinnerung an fällige Prüfung."""
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""
    datum  = idv_row["naechste_pruefung"] if hasattr(idv_row, "__getitem__") else ""

    subject = f"[idvault] Prüfung fällig: {idv_id} – {name}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;">
    <h2 style="color:#fd7e14;">idvault – Prüfung fällig</h2>
    <p>Die Prüfung für folgendes IDV ist fällig oder überfällig:</p>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:6px;font-weight:bold;width:160px;">IDV-ID</td>
          <td style="padding:6px;">{idv_id}</td></tr>
      <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Bezeichnung</td>
          <td style="padding:6px;">{name}</td></tr>
      <tr><td style="padding:6px;font-weight:bold;">Fällig am</td>
          <td style="padding:6px;">{datum[:10] if datum else '–'}</td></tr>
    </table>
    <p style="color:#6c757d;font-size:12px;margin-top:30px;">
      Diese Nachricht wurde automatisch von idvault gesendet.
    </p>
    </body></html>
    """
    text = f"Prüfung fällig: {idv_id} – {name}\nFällig am: {datum[:10] if datum else '–'}"
    return send_mail(db, responsible_email, subject, html, text)


def notify_measure_overdue(db, massnahme_row, responsible_email: str) -> bool:
    """Eskalation für überfällige Maßnahme."""
    titel  = massnahme_row["titel"] if hasattr(massnahme_row, "__getitem__") else str(massnahme_row)
    faellig = massnahme_row["faellig_am"] if hasattr(massnahme_row, "__getitem__") else ""

    subject = f"[idvault] Überfällige Maßnahme: {titel}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;font-size:14px;">
    <h2 style="color:#dc3545;">idvault – Überfällige Maßnahme</h2>
    <p>Die folgende Maßnahme ist überfällig:</p>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="padding:6px;font-weight:bold;width:160px;">Titel</td>
          <td style="padding:6px;">{titel}</td></tr>
      <tr style="background:#f8f9fa"><td style="padding:6px;font-weight:bold;">Fällig am</td>
          <td style="padding:6px;">{faellig[:10] if faellig else '–'}</td></tr>
    </table>
    <p style="color:#6c757d;font-size:12px;margin-top:30px;">
      Diese Nachricht wurde automatisch von idvault gesendet.
    </p>
    </body></html>
    """
    text = f"Überfällige Maßnahme: {titel}\nFällig am: {faellig[:10] if faellig else '–'}"
    return send_mail(db, responsible_email, subject, html, text)
