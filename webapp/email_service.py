"""
IDVScope – E-Mail-Benachrichtigungs-Service
==========================================
Sendet transaktionale E-Mails via SMTP (TLS/STARTTLS).
Konfiguration wird aus der app_settings-Tabelle gelesen.

Alle E-Mail-Vorlagen (Betreff + HTML-Body) sind über die Admin-Oberfläche
konfigurierbar. Platzhalter werden im Format {name} ersetzt.

Die gesamte Konfiguration (Host, Port, Benutzer, Passwort, Absender, TLS)
wird ausschließlich aus der Datenbank (app_settings) gelesen.
"""

import os
import html as _html
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from typing import Optional

log = logging.getLogger("idvscope.email")


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

    Werte ohne ``enc:``-Präfix werden als leer behandelt (kein
    Klartext-Fallback).
    """
    if not stored or not stored.startswith(_ENC_PREFIX):
        return ""
    try:
        token = stored[len(_ENC_PREFIX):]
        return _smtp_fernet().decrypt(token.encode()).decode()
    except Exception as exc:
        log.warning("SMTP-Passwort kann nicht entschlüsselt werden: %s", exc)
        return ""


def _parse_tls_mode(value: str) -> str:
    """Normalisiert smtp_tls-Werte zu 'starttls', 'ssl' oder 'none'."""
    return value if value in ("starttls", "ssl", "none") else "starttls"


def _get_smtp_config(db) -> dict:
    """Liest SMTP-Einstellungen ausschließlich aus der Datenbank (app_settings)."""
    try:
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
        cfg  = {r["key"]: r["value"] for r in rows}
    except Exception as exc:
        log.error("SMTP-Konfiguration konnte nicht aus DB gelesen werden: %s", exc)
        cfg  = {}

    return {
        "host":     cfg.get("smtp_host",  ""),
        "port":     int(cfg.get("smtp_port",  587)),
        "user":     cfg.get("smtp_user",  ""),
        "password": _decrypt_smtp_password(cfg.get("smtp_password", "")),
        "from":     cfg.get("smtp_from",  ""),
        "tls_mode": _parse_tls_mode(cfg.get("smtp_tls", "starttls")),
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


def _open_smtp(cfg: dict) -> smtplib.SMTP:
    """Öffnet eine SMTP-Verbindung gemäß tls_mode.

    tls_mode 'starttls' : SMTP + STARTTLS (typisch Port 587)
    tls_mode 'ssl'      : SMTP_SSL        (typisch Port 465)
    tls_mode 'none'     : Plain SMTP      (typisch Port 25, Relay ohne TLS)
    """
    mode = cfg.get("tls_mode", "starttls")
    if mode == "ssl":
        return smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)
    smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
    if mode == "starttls":
        smtp.starttls()
    return smtp


def send_mail(db, to: str | list[str], subject: str,
              body_html: str, body_text: str = "") -> bool:
    """Sendet eine E-Mail. Gibt True bei Erfolg zurück."""
    cfg = _get_smtp_config(db)
    if not cfg["host"] or not cfg["from"]:
        log.warning(
            "E-Mail nicht konfiguriert: smtp_host=%r smtp_from=%r",
            cfg["host"], cfg["from"]
        )
        return False

    recipients = [to] if isinstance(to, str) else to
    recipients = [r for r in recipients if r and "@" in r]
    if not recipients:
        log.warning("Keine gültigen Empfänger-Adressen.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("IDVScope", cfg["from"]))
    msg["To"]      = ", ".join(recipients)

    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    smtp = None
    try:
        smtp = _open_smtp(cfg)
        if cfg["user"] and cfg["password"]:
            smtp.login(cfg["user"], cfg["password"])
        smtp.sendmail(cfg["from"], recipients, msg.as_string())
        log.info("E-Mail gesendet an %s: %s", recipients, subject)
        _smtp_log(db, recipients, subject, True)
        return True
    except Exception as exc:
        err = str(exc)
        log.error("E-Mail-Fehler: %s", err)
        _smtp_log(db, recipients, subject, False, err)
        return False
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


def send_smtp_test(db, to_email: str, *,
                   host: str = None, port: int = None,
                   user: str = None, password: str = None,
                   smtp_from: str = None, tls_mode: str = None) -> tuple[bool, str]:
    """Sendet eine Test-E-Mail und gibt (Erfolg, Meldung) zurück.

    Optionale Keyword-Argumente überschreiben die gespeicherten DB/Env-Werte.
    Ist ``password`` None, wird das gespeicherte DB-Passwort verwendet.
    ``tls_mode`` akzeptiert 'starttls', 'ssl' oder 'none'.
    """
    cfg = _get_smtp_config(db)
    if host      is not None: cfg["host"]     = host
    if port      is not None: cfg["port"]     = port
    if user      is not None: cfg["user"]     = user
    if password  is not None: cfg["password"] = password
    if smtp_from is not None: cfg["from"]     = smtp_from
    if tls_mode  is not None: cfg["tls_mode"] = tls_mode

    if not cfg["host"]:
        return False, "SMTP-Host ist nicht konfiguriert."
    if not cfg["from"]:
        return False, "Absenderadresse (smtp_from) ist nicht konfiguriert."
    if not to_email or "@" not in to_email:
        return False, "Ungültige Empfänger-Adresse."

    subject = "[IDVScope] SMTP-Verbindungstest"
    body_html = _render_email(
        accent="success",
        kind_label="SMTP-Test",
        headline="SMTP-Test erfolgreich",
        intro_html=(
            "<p>Diese E-Mail wurde automatisch als Verbindungstest gesendet.</p>"
            "<p>Die SMTP-Konfiguration funktioniert korrekt.</p>"
        ),
    )

    recipients = [to_email]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr(("IDVScope", cfg["from"]))
    msg["To"]      = to_email
    msg.attach(MIMEText("IDVScope – SMTP-Test erfolgreich.\n\nDiese E-Mail wurde automatisch als Verbindungstest gesendet.", "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    smtp = None
    try:
        smtp = _open_smtp(cfg)
        if cfg["user"] and cfg["password"]:
            smtp.login(cfg["user"], cfg["password"])
        smtp.sendmail(cfg["from"], recipients, msg.as_string())
        log.info("SMTP-Test-E-Mail gesendet an %s", to_email)
        _smtp_log(db, recipients, subject, True)
        return True, f"Test-E-Mail erfolgreich gesendet an {to_email}."
    except Exception as exc:
        err = str(exc)
        log.error("SMTP-Test-Fehler: %s", err)
        _smtp_log(db, recipients, subject, False, err)
        return False, f"Fehler: {err}"
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Zentrale Template-Mechanik
# ---------------------------------------------------------------------------

def _replace_placeholders(template: str, placeholders: dict) -> str:
    """Ersetzt {name}-Platzhalter im Template."""
    for key, val in placeholders.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def _load_template(db, tpl_key: str, default_subject: str, default_body_html: str,
                   placeholders: dict) -> tuple[str, str, str]:
    """Lädt Betreff und Body für einen Template-Key aus app_settings.

    Gibt (subject, body_html, body_text) mit ersetzten Platzhaltern zurück.
    Im Modus 'text' wird der gespeicherte Klartext in ein minimales HTML gewrappt.
    """
    subject_key = f"email_tpl_{tpl_key}_subject"
    body_key    = f"email_tpl_{tpl_key}_body"
    mode_key    = f"email_tpl_{tpl_key}_mode"

    try:
        rows = {r["key"]: r["value"] for r in db.execute(
            "SELECT key, value FROM app_settings WHERE key IN (?,?,?)",
            (subject_key, body_key, mode_key)
        ).fetchall()}
    except Exception:
        rows = {}

    subject     = rows.get(subject_key) or default_subject
    mode        = rows.get(mode_key) or "html"
    stored_body = rows.get(body_key) or ""

    if mode == "text":
        body_text = stored_body or _strip_html_tags(default_body_html)
        body_text = _replace_placeholders(body_text, placeholders)
        body_html = (
            "<html><body style=\"font-family:Arial,sans-serif;font-size:14px\">"
            "<p style=\"white-space:pre-wrap\">" + _html.escape(body_text) + "</p>"
            "</body></html>"
        )
        return (_replace_placeholders(subject, placeholders), body_html, body_text)

    body_html = stored_body or default_body_html
    body_text = _strip_html_tags(body_html)
    return (
        _replace_placeholders(subject, placeholders),
        _replace_placeholders(body_html, placeholders),
        _replace_placeholders(body_text, placeholders),
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
# Einheitlicher Look (Volksbank-CI-nah, deckt sich mit dem Web-UI)
# ---------------------------------------------------------------------------
#
# Alle E-Mails verwenden ein gemeinsames HTML-Gerüst:
#   - dunkler Header (Navy) mit gelbem Akzentstreifen analog zur Sidebar
#   - weisse Inhaltsbox, Slate-Typografie, einheitliche Tabellen
#   - farbiger Status-Bar links neben der Headline (info/success/warning/danger)
#   - optionaler primärer CTA-Button (Navy mit gelbem Unterstrich)
#   - Footer-Hinweis "automatisch von IDVScope gesendet"
#
# Die Funktion erzeugt komplettes HTML inklusive Platzhalter wie {idv_id};
# Platzhalter werden später durch ``_load_template`` ersetzt. Dadurch bleibt
# das Admin-UI (Override pro Vorlage) unverändert nutzbar.

_BRAND = {
    "navy_900":  "#152342",
    "navy_800":  "#1b2a4e",
    "navy_950":  "#0f1b34",
    "amber":     "#f59e0b",
    "slate_50":  "#f8fafc",
    "slate_100": "#f1f5f9",
    "slate_200": "#e2e8f0",
    "slate_300": "#cbd5e1",
    "slate_500": "#64748b",
    "slate_700": "#334155",
    "slate_900": "#0f172a",
}

_ACCENTS = {
    "info":    {"bar": "#1d4ed8"},
    "success": {"bar": "#15803d"},
    "warning": {"bar": "#b45309"},
    "danger":  {"bar": "#b91c1c"},
}


def _render_email(*, accent: str, kind_label: str, headline: str,
                  intro_html: str = "",
                  rows: list | None = None,
                  extra_html: str = "",
                  cta_label: str = "", cta_url: str = "") -> str:
    """Rendert eine E-Mail im einheitlichen IDVScope-Layout.

    ``accent``     – ``info`` | ``success`` | ``warning`` | ``danger``
    ``kind_label`` – kurzer Text oben rechts im Header (z.B. "Freigabe")
    ``headline``   – Hauptüberschrift
    ``intro_html`` – Einleitender Absatz (HTML erlaubt; Platzhalter zulässig)
    ``rows``       – Liste von ``(label, value)`` für die Datentabelle
    ``extra_html`` – Zusätzlicher Inhalt unterhalb der Tabelle (z.B. Listen)
    ``cta_label`` / ``cta_url`` – primärer CTA-Button
    """
    a = _ACCENTS.get(accent, _ACCENTS["info"])
    rows_html = ""
    if rows:
        cells = ""
        for i, (label, value) in enumerate(rows):
            bg = _BRAND["slate_50"] if i % 2 else "#ffffff"
            cells += (
                f'<tr>'
                f'<td style="padding:8px 12px;font-weight:600;color:{_BRAND["slate_700"]};'
                f'width:170px;background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">'
                f'{label}</td>'
                f'<td style="padding:8px 12px;color:{_BRAND["slate_900"]};'
                f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">'
                f'{value}</td>'
                f'</tr>'
            )
        rows_html = (
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;width:100%;'
            f'border:1px solid {_BRAND["slate_200"]};border-radius:6px;'
            'font-size:14px;margin:18px 0;overflow:hidden;">'
            f'{cells}</table>'
        )

    cta_html = ""
    if cta_label and cta_url:
        cta_html = (
            f'<p style="margin:24px 0 4px 0;text-align:center;">'
            f'<a href="{cta_url}" '
            f'style="background:{_BRAND["navy_900"]};color:#ffffff;'
            f'padding:12px 28px;border-radius:6px;text-decoration:none;'
            f'font-weight:600;font-size:15px;display:inline-block;'
            f'border-bottom:3px solid {_BRAND["amber"]};">'
            f'{cta_label}</a></p>'
        )

    intro_block = (
        f'<div style="font-size:14px;line-height:1.55;color:{_BRAND["slate_900"]};">'
        f'{intro_html}</div>'
    ) if intro_html else ""

    return f"""\
<html><body style="margin:0;padding:0;background:{_BRAND["slate_100"]};\
font-family:Arial,Helvetica,sans-serif;color:{_BRAND["slate_900"]};">
<table role="presentation" cellspacing="0" cellpadding="0" align="center" \
style="border-collapse:collapse;background:#ffffff;width:100%;max-width:640px;\
margin:24px auto;border:1px solid {_BRAND["slate_200"]};border-radius:8px;\
overflow:hidden;">
  <tr><td style="background:{_BRAND["navy_900"]};\
background-image:linear-gradient(180deg,{_BRAND["navy_800"]} 0%,{_BRAND["navy_950"]} 100%);\
padding:18px 24px;border-bottom:3px solid {_BRAND["amber"]};">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
      <tr>
        <td style="font-size:13px;font-weight:700;letter-spacing:0.08em;\
color:{_BRAND["amber"]};text-transform:uppercase;">IDVScope</td>
        <td style="text-align:right;font-size:12px;color:#dbe4f0;\
text-transform:uppercase;letter-spacing:0.05em;">{kind_label}</td>
      </tr>
    </table>
  </td></tr>
  <tr><td style="padding:24px;">
    <div style="border-left:4px solid {a['bar']};padding:2px 0 2px 14px;margin-bottom:18px;">
      <h2 style="margin:0;font-size:20px;color:{_BRAND["slate_900"]};font-weight:700;">\
{headline}</h2>
    </div>
    {intro_block}
    {rows_html}
    {extra_html}
    {cta_html}
  </td></tr>
  <tr><td style="background:{_BRAND["slate_50"]};padding:14px 24px;\
border-top:1px solid {_BRAND["slate_200"]};font-size:12px;\
color:{_BRAND["slate_500"]};text-align:center;">
    Diese Nachricht wurde automatisch von IDVScope gesendet.
  </td></tr>
</table>
</body></html>"""


def _row_get(row, key, default=None):
    """Sicherer Zugriff auf ein Feld eines ``sqlite3.Row`` oder Dicts.

    Gibt ``default`` zurück, wenn die Spalte nicht existiert oder der
    Zugriff fehlschlägt (etwa weil das Test-Mock kein Mapping ist).
    """
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return value if value is not None else default


def _idv_link(base_url: str, idv_db_id) -> str:
    """Baut die URL zur IDV-Detailseite. Leerer String, wenn nicht möglich."""
    if not base_url or not idv_db_id:
        return ""
    return f"{base_url.rstrip('/')}/eigenentwicklung/{int(idv_db_id)}"


def _inject_cta(html: str, label: str, url: str) -> str:
    """Hängt einen CTA-Button vor das schließende </body> an.

    Wird genutzt, wenn die Vorlage selbst keinen CTA enthält (z.B. weil sie
    aus dem Admin-UI als Klartext oder als Override gesetzt wurde) — der
    Empfänger soll trotzdem direkt zur IDV-Doku springen können.
    """
    if not label or not url:
        return html
    cta = (
        f'<p style="margin:24px 0 4px 0;text-align:center;">'
        f'<a href="{url}" '
        f'style="background:{_BRAND["navy_900"]};color:#ffffff;'
        f'padding:12px 28px;border-radius:6px;text-decoration:none;'
        f'font-weight:600;font-size:15px;display:inline-block;'
        f'border-bottom:3px solid {_BRAND["amber"]};">'
        f'{label}</a></p>'
    )
    if "</body>" in html:
        return html.replace("</body>", cta + "</body>")
    return html + cta


# ---------------------------------------------------------------------------
# Default-Templates (Fallback, wenn nichts in app_settings konfiguriert)
# ---------------------------------------------------------------------------

_DEFAULTS = {}

# ── 1. Neue Datei erkannt ─────────────────────────────────────────────────

_DEFAULTS["neue_datei_subject"] = "[IDVScope] Neue IDV-Datei erkannt: {dateiname}"
_DEFAULTS["neue_datei_body"] = _render_email(
    accent="info",
    kind_label="Scanner",
    headline="Neue Datei erkannt",
    intro_html=(
        "<p>Der IDVScope-Scanner hat eine neue Datei entdeckt, die noch "
        "nicht im IDV-Register erfasst ist.</p>"
    ),
    rows=[
        ("Dateiname", "{dateiname}"),
        ("Pfad", '<span style="font-family:monospace;font-size:12px;">{pfad}</span>'),
        ("Erstmals erkannt", "{erkannt_am}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Bitte melden Sie sich in IDVScope an und "
        "erfassen Sie die Datei im IDV-Register.</p>"
    ),
)

# ── 2. Prüfung fällig ────────────────────────────────────────────────────

_DEFAULTS["pruefung_faellig_subject"] = "[IDVScope] Prüfung fällig: {idv_id} – {bezeichnung}"
_DEFAULTS["pruefung_faellig_body"] = _render_email(
    accent="warning",
    kind_label="Prüfung",
    headline="Prüfung fällig",
    intro_html="<p>Die Prüfung für folgendes IDV ist fällig oder überfällig:</p>",
    rows=[
        ("IDV-ID", "{idv_id}"),
        ("Bezeichnung", "{bezeichnung}"),
        ("Fällig am", "{faellig_am}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Bitte melden Sie sich in IDVScope an und "
        "führen Sie die Prüfung durch.</p>"
    ),
)

# ── 3. Freigabe-Schritt offen ────────────────────────────────────────────

_DEFAULTS["freigabe_schritt_subject"] = "[IDVScope] Freigabe-Schritt offen: {schritt} – {idv_id}"
_DEFAULTS["freigabe_schritt_body"] = _render_email(
    accent="info",
    kind_label="Test & Freigabe",
    headline="Freigabe-Schritt zur Bearbeitung",
    intro_html=(
        "<p>Für die folgende IDV steht ein Freigabe-Schritt zur Bearbeitung "
        "bereit:</p>"
    ),
    rows=[
        ("IDV-ID", "{idv_id}"),
        ("Bezeichnung", "{bezeichnung}"),
        ("Schritt", '<strong style="color:#1d4ed8;">{schritt}</strong>'),
        ("Versionskommentar", "<em>{versionskommentar}</em>"),
    ],
    extra_html=(
        '<p style="font-size:12px;color:#64748b;margin-top:6px;">'
        "Bitte melden Sie sich in IDVScope an und schließen Sie den Schritt "
        "ab. Hinweis: Gemäß Funktionstrennung darf der Entwickler der IDV "
        "keine Freigabe-Schritte abschließen.</p>"
    ),
)

# ── 4. IDV freigegeben (alle 4 Schritte erledigt) ────────────────────────

_DEFAULTS["freigabe_abgeschlossen_subject"] = "[IDVScope] IDV freigegeben: {idv_id} – {bezeichnung}"
_DEFAULTS["freigabe_abgeschlossen_body"] = _render_email(
    accent="success",
    kind_label="Freigabe",
    headline="IDV freigegeben",
    intro_html=(
        "<p>Alle vier Freigabe-Schritte wurden erfolgreich abgeschlossen:</p>"
    ),
    rows=[
        ("IDV-ID", "{idv_id}"),
        ("Bezeichnung", "{bezeichnung}"),
    ],
    extra_html=(
        '<ul style="margin:12px 0 0 0;padding-left:20px;font-size:14px;'
        'line-height:1.6;">'
        "<li>Fachlicher Test – erledigt</li>"
        "<li>Technischer Test – erledigt</li>"
        "<li>Fachliche Abnahme – erledigt</li>"
        "<li>Technische Abnahme – erledigt</li>"
        "</ul>"
        '<p style="margin-top:16px;">Die IDV wurde auf Status '
        "<strong>Freigegeben</strong> und Dokumentationsstatus "
        "<strong>Dokumentiert</strong> gesetzt.</p>"
    ),
)

# ── 5. Bewertungsanforderung (an Datei-Ersteller) ────────────────────────

_DEFAULTS["bewertung_subject"] = "[IDVScope] Bitte um Bewertung: {dateiname}"
_DEFAULTS["bewertung_body"] = _render_email(
    accent="info",
    kind_label="Bewertung",
    headline="Bewertung angefordert",
    intro_html=(
        "<p>Sehr geehrte/r {ersteller},</p>"
        "<p>die folgende Datei wurde vom IDVScope-Scanner erkannt und ist "
        "Ihnen als Ersteller/Eigentümer zugeordnet. Bitte bewerten Sie, ob "
        "diese Datei als <strong>Individuelle Datenverarbeitung (IDV)</strong> "
        "im Sinne von MaRisk AT 7.2 einzustufen ist.</p>"
    ),
    rows=[
        ("Dateiname", "{dateiname}"),
        ("Pfad", '<span style="font-family:monospace;font-size:12px;">{pfad}</span>'),
        ("Formeln", "{formelanzahl}"),
        ("Makros", "{makros}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Bitte melden Sie sich in IDVScope an und "
        "nehmen Sie die Bewertung vor.</p>"
    ),
)

# ── 7. Pool-Freigabeschritt wartet auf Claim (täglicher Reminder) ───────

_DEFAULTS["freigabe_pool_reminder_subject"] = "[IDVScope] Freigabeschritt wartet: {schritt} – {idv_id}"
_DEFAULTS["freigabe_pool_reminder_body"] = _render_email(
    accent="info",
    kind_label="Pool-Schritt",
    headline="Pool-Freigabeschritt wartet auf Übernahme",
    intro_html=(
        "<p>Ein Freigabeschritt, der Ihrem Pool <strong>{pool_name}</strong> "
        "zugewiesen ist, wartet seit {wartet_seit_tage} Tag(en) auf "
        "Bearbeitung und wurde noch von niemandem übernommen.</p>"
    ),
    rows=[
        ("IDV-ID", "{idv_id}"),
        ("Bezeichnung", "{bezeichnung}"),
        ("Schritt", '<strong style="color:#1d4ed8;">{schritt}</strong>'),
        ("Pool", "{pool_name}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Bitte melden Sie sich in IDVScope an und "
        "klicken Sie auf <em>„Ich übernehme\"</em>, damit die Aufgabe nicht "
        "weiter liegen bleibt. Sobald ein Pool-Mitglied den Schritt "
        "übernimmt, wird dieser Reminder für alle anderen Mitglieder "
        "eingestellt.</p>"
    ),
)

# ── 7b. Sammelbenachrichtigung an Owner (Self-Service) ──────────────────

_DEFAULTS["owner_digest_subject"] = "[IDVScope] Offene Scanner-Funde in Ihrem Bereich ({anzahl})"
_DEFAULTS["owner_digest_body"] = _render_email(
    accent="info",
    kind_label="Self-Service",
    headline="Ihre offenen Scanner-Funde",
    intro_html=(
        "<p>Sehr geehrte/r {empfaenger},</p>"
        "<p>der IDVScope-Scanner hat <strong>{anzahl}</strong> Datei(en) in "
        "Ihrem Zugriff entdeckt, die noch keiner IDV zugeordnet sind. Bitte "
        "entscheiden Sie je Datei, ob sie für die Registrierung vorgemerkt "
        "oder ignoriert werden soll.</p>"
    ),
    cta_label="Meine Funde öffnen →",
    cta_url="{link}",
    extra_html=(
        '<p style="color:#64748b;font-size:12px;margin-top:8px;text-align:center;">'
        "Der Link ist <strong>7 Tage</strong> gültig und öffnet eine "
        "Minimalansicht ohne Anmeldung. Die fachliche IDV-Einordnung "
        "übernimmt weiterhin der IDV-Koordinator.</p>"
    ),
)

# ── 8. IDV unvollständig ─────────────────────────────────────────────────

_DEFAULTS["idv_incomplete_reminder_subject"] = "[IDVScope] IDV unvollständig: {idv_id} – {bezeichnung}"
_DEFAULTS["idv_incomplete_reminder_body"] = _render_email(
    accent="warning",
    kind_label="Nachpflege",
    headline="Nachpflege erforderlich",
    intro_html=(
        "<p>die folgende Eigenentwicklung ist über die Schnell-Anlage erfasst "
        "und noch nicht vollständig dokumentiert. Bitte pflegen Sie die "
        "fehlenden Angaben innerhalb der nächsten 14 Tage nach.</p>"
    ),
    rows=[
        ("IDV-ID", "{idv_id}"),
        ("Bezeichnung", "{bezeichnung}"),
        ("Vollständigkeit", '<strong style="color:#b45309;">{score} %</strong>'),
        ("Noch offen", "{missing}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Der Freigabe-Workflow kann erst gestartet "
        "werden, sobald der Vollständigkeits-Score 100 % erreicht.</p>"
    ),
)

# ── 9. Überfällige Maßnahme ──────────────────────────────────────────────

_DEFAULTS["massnahme_ueberfaellig_subject"] = "[IDVScope] Überfällige Maßnahme: {titel}"
_DEFAULTS["massnahme_ueberfaellig_body"] = _render_email(
    accent="danger",
    kind_label="Maßnahme",
    headline="Überfällige Maßnahme",
    intro_html="<p>Die folgende Maßnahme ist überfällig:</p>",
    rows=[
        ("Titel", "{titel}"),
        ("IDV", "{idv_id} – {bezeichnung}"),
        ("Fällig am", "{faellig_am}"),
    ],
    extra_html=(
        "<p style=\"margin-top:6px;\">Bitte melden Sie sich in IDVScope an und "
        "bearbeiten Sie die Maßnahme.</p>"
    ),
)


# ---------------------------------------------------------------------------
# Template-Registry (für Admin-UI und Validierung)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Empfänger-Rollen-Katalog
# ---------------------------------------------------------------------------
#
# Pro Mail-Vorlage lässt sich rollenbasiert konfigurieren, wer die Mail
# bekommt. Der Katalog beschreibt sämtliche bekannten Rollen mit Label und
# kurzer Erläuterung — das Admin-UI macht damit transparent, welche
# Personen-Kreise eine Vorlage adressieren kann.
#
# Welche Rollen für eine konkrete Vorlage *konfigurierbar* sind, regelt der
# Eintrag ``recipient_roles`` in ``EMAIL_TEMPLATES``. Default-Auswahl steht
# in ``default_recipients``. Gespeichert wird die aktive Auswahl als
# komma-separierte Rollenliste in ``app_settings`` unter
# ``email_recipients_{tpl_key}``.
RECIPIENT_ROLES = {
    "idv_administrator": {
        "label": "IDV-Administratoren",
        "hint": "Alle aktiven Personen mit Rolle „IDV-Administrator“.",
    },
    "idv_koordinator": {
        "label": "IDV-Koordinatoren",
        "hint": "Alle aktiven Personen mit Rolle „IDV-Koordinator“.",
    },
    "idv_entwickler": {
        "label": "IDV-Entwickler",
        "hint": "Die am betreffenden IDV als Entwickler hinterlegte Person.",
    },
    "fachverantwortlicher": {
        "label": "Fachverantwortlicher",
        "hint": "Die am betreffenden IDV als Fachverantwortlicher hinterlegte Person.",
    },
    "schritt_verantwortlicher": {
        "label": "Schritt-Verantwortlicher",
        "hint": "Die für den jeweiligen Freigabe-Schritt zugewiesene Person.",
    },
    "freigabe_pool": {
        "label": "Pool-Mitglieder",
        "hint": "Aktive Mitglieder des für den Schritt zuständigen Freigabe-Pools.",
    },
    "datei_ersteller": {
        "label": "Datei-Ersteller / -Eigentümer",
        "hint": "Ersteller/Eigentümer der Datei laut file_owner / office_author.",
    },
    "massnahme_verantwortlicher": {
        "label": "Maßnahmen-Verantwortlicher",
        "hint": "Die für die Maßnahme verantwortliche Person (oder aktive Vertretung).",
    },
    "self_service_owner": {
        "label": "Self-Service-Empfänger",
        "hint": "Eigentümer der Funde laut file_owner-Mapping (Self-Service).",
    },
}


EMAIL_TEMPLATES = {
    "neue_datei": {
        "label": "Neue Datei erkannt",
        "placeholders": ["dateiname", "pfad", "erkannt_am"],
        "recipient_roles": ["idv_administrator", "idv_koordinator"],
        "default_recipients": ["idv_administrator", "idv_koordinator"],
    },
    "pruefung_faellig": {
        "label": "Prüfung fällig",
        "placeholders": ["idv_id", "bezeichnung", "faellig_am"],
        "recipient_roles": [
            "fachverantwortlicher", "idv_entwickler",
            "idv_koordinator", "idv_administrator",
        ],
        "default_recipients": ["fachverantwortlicher"],
    },
    "freigabe_schritt": {
        "label": "Freigabe-Schritt offen",
        "placeholders": ["idv_id", "bezeichnung", "schritt", "versionskommentar"],
        "recipient_roles": [
            "schritt_verantwortlicher", "freigabe_pool",
            "idv_koordinator", "idv_administrator",
        ],
        "default_recipients": [
            "schritt_verantwortlicher", "freigabe_pool",
            "idv_koordinator", "idv_administrator",
        ],
    },
    "freigabe_abgeschlossen": {
        "label": "IDV freigegeben (alle Schritte erledigt)",
        "placeholders": ["idv_id", "bezeichnung"],
        "recipient_roles": [
            "idv_entwickler", "fachverantwortlicher",
            "idv_koordinator", "idv_administrator",
        ],
        "default_recipients": [
            "idv_entwickler", "fachverantwortlicher",
            "idv_koordinator", "idv_administrator",
        ],
    },
    "bewertung": {
        "label": "Bewertungsanforderung an Datei-Ersteller",
        "placeholders": ["dateiname", "pfad", "ersteller", "formelanzahl", "makros"],
        "recipient_roles": ["datei_ersteller"],
        "default_recipients": ["datei_ersteller"],
    },
    "massnahme_ueberfaellig": {
        "label": "Überfällige Maßnahme",
        "placeholders": ["titel", "faellig_am", "idv_id", "bezeichnung"],
        "recipient_roles": [
            "massnahme_verantwortlicher",
            "idv_koordinator", "idv_administrator",
        ],
        "default_recipients": ["massnahme_verantwortlicher"],
    },
    "freigabe_pool_reminder": {
        "label": "Pool-Freigabeschritt wartet auf Übernahme (täglicher Reminder)",
        "placeholders": ["idv_id", "bezeichnung", "schritt", "pool_name", "wartet_seit_tage"],
        "recipient_roles": ["freigabe_pool"],
        "default_recipients": ["freigabe_pool"],
    },
    "owner_digest": {
        "label": "Sammelbenachrichtigung: offene Scanner-Funde (Self-Service)",
        "placeholders": ["empfaenger", "anzahl", "link"],
        "recipient_roles": ["self_service_owner"],
        "default_recipients": ["self_service_owner"],
    },
    "idv_incomplete_reminder": {
        "label": "IDV unvollständig – Nachpflege erforderlich (Schnell-Anlage)",
        "placeholders": ["idv_id", "bezeichnung", "score", "missing"],
        "recipient_roles": [
            "fachverantwortlicher", "idv_entwickler",
            "idv_koordinator", "idv_administrator",
        ],
        "default_recipients": ["fachverantwortlicher"],
    },
}


# ---------------------------------------------------------------------------
# Benachrichtigungs-Funktionen
# ---------------------------------------------------------------------------

def _is_notify_enabled(db, tpl_key: str) -> bool:
    """Prüft ob der Mailversand für diesen Template-Typ in den Einstellungen aktiviert ist.

    Liest den Key ``notify_enabled_{tpl_key}`` aus app_settings.
    Fehlt der Eintrag, gilt die Benachrichtigung als aktiviert (sicherer Default).
    """
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=?",
            (f"notify_enabled_{tpl_key}",)
        ).fetchone()
        if row is not None:
            return row["value"] == "1"
        return True
    except Exception:
        return True


def get_configured_recipient_roles(db, tpl_key: str) -> list[str]:
    """Liest die für die Vorlage konfigurierten Empfänger-Rollen.

    Speicherort: ``app_settings.email_recipients_{tpl_key}`` als
    komma-separierte Rollenliste.

    Fehlt der Eintrag komplett, greifen die ``default_recipients`` der
    Vorlage. Ein vorhandener, aber leerer Wert bedeutet bewusst:
    *keine* Empfänger (Versand effektiv aus).
    """
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=?",
            (f"email_recipients_{tpl_key}",),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return list(EMAIL_TEMPLATES.get(tpl_key, {}).get("default_recipients", []))
    raw = (row["value"] or "").strip()
    if not raw:
        return []
    allowed = set(EMAIL_TEMPLATES.get(tpl_key, {}).get("recipient_roles", []))
    return [r.strip() for r in raw.split(",")
            if r.strip() and (not allowed or r.strip() in allowed)]


def filter_emails_by_configured_roles(db, tpl_key: str,
                                      role_emails: dict) -> list[str]:
    """Reduziert eine ``{role_key: iterable[email]}``-Zuordnung auf jene
    Rollen, die für diese Vorlage in den Einstellungen aktiviert sind.

    Liefert eine sortierte, deduplizierte Liste echter E-Mail-Adressen
    (``"@"``-Heuristik analog zu ``send_mail``).
    """
    enabled = set(get_configured_recipient_roles(db, tpl_key))
    out: set[str] = set()
    for role, emails in (role_emails or {}).items():
        if role not in enabled:
            continue
        for e in emails or []:
            if e and "@" in e:
                out.add(e)
    return sorted(out)


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

    subject, html, text = _load_template(
        db, "neue_datei",
        _DEFAULTS["neue_datei_subject"],
        _DEFAULTS["neue_datei_body"],
        placeholders,
    )
    return send_mail(db, responsible_emails, subject, html, text)


def notify_review_due(db, idv_row, recipient_emails) -> bool:
    """Erinnerung an fällige Prüfung.

    ``recipient_emails`` darf entweder ein einzelner String oder eine
    Liste sein — wird durchgereicht an ``send_mail``.
    """
    if not _is_notify_enabled(db, "pruefung_faellig"):
        return False
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""
    datum  = idv_row["naechste_pruefung"] if hasattr(idv_row, "__getitem__") else ""
    idv_db_id = _row_get(idv_row, "id")

    placeholders = {
        "idv_id":      idv_id,
        "bezeichnung": name,
        "faellig_am":  datum[:10] if datum else "–",
    }

    subject, html, text = _load_template(
        db, "pruefung_faellig",
        _DEFAULTS["pruefung_faellig_subject"],
        _DEFAULTS["pruefung_faellig_body"],
        placeholders,
    )

    link = _idv_link(get_app_base_url(db), idv_db_id)
    if link:
        html = _inject_cta(html, "Zur IDV-Doku öffnen →", link)
        text = _strip_html_tags(html)

    return send_mail(db, recipient_emails, subject, html, text)


def notify_freigabe_schritt(db, idv_row, schritt: str,
                            recipient_emails: list,
                            versions_kommentar: str = None,
                            action_url: str = None) -> bool:
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

    subject, html, text = _load_template(
        db, "freigabe_schritt",
        _DEFAULTS["freigabe_schritt_subject"],
        _DEFAULTS["freigabe_schritt_body"],
        placeholders,
    )

    if action_url:
        html = _inject_cta(html, f'Schritt „{schritt}" öffnen →', action_url)

    text = _strip_html_tags(html)
    return send_mail(db, recipient_emails, subject, html, text)


def notify_silent_release_supervisor(db, idv_db_id: int, magic_link: str,
                                     entwickler_name: str = "") -> bool:
    """Mail an den Fachverantwortlichen mit Sicht-Freigabe-Link (Issue #351).

    Bewusst schlank gehalten: nutzt keine eigene Template-Konfiguration,
    sondern baut die Mail direkt. Wer die Mail anpassen will, kann
    spaeter eine ``app_settings``-Vorlage einfuehren.
    """
    row = db.execute(
        "SELECT r.idv_id, r.bezeichnung, p.email, p.vorname, p.nachname "
        "  FROM idv_register r "
        "  LEFT JOIN persons p ON p.id = r.fachverantwortlicher_id "
        " WHERE r.id = ?",
        (idv_db_id,),
    ).fetchone()
    if not row or not row["email"]:
        return False
    subject = f"[IDVScope] Sicht-Freigabe erforderlich – {row['idv_id']}"
    entwickler_suffix = f" ({entwickler_name})" if entwickler_name else ""
    html = _render_email(
        accent="info",
        kind_label="Sicht-Freigabe",
        headline="Sicht-Freigabe erforderlich",
        intro_html=(
            f"<p>Hallo {row['vorname']} {row['nachname']},</p>"
            f"<p>für die nicht-wesentliche Eigenentwicklung "
            f"<strong>{row['bezeichnung']}</strong> ({row['idv_id']}) liegt eine "
            f"Selbstzertifizierung des Entwicklers{entwickler_suffix} vor. "
            f"Bitte bestätigen Sie die Sicht-Freigabe per Klick.</p>"
        ),
        cta_label="Sicht-Freigabe öffnen",
        cta_url=magic_link,
        extra_html=(
            '<p style="font-size:12px;color:#64748b;text-align:center;margin-top:8px;">'
            "Der Link ist 7 Tage gültig.</p>"
        ),
    )
    text = _strip_html_tags(html)
    return send_mail(db, row["email"], subject, html, text)


def notify_self_service_escalation(db, recipient_email: str, recipient_name: str,
                                   stage: str, days: int,
                                   owner_name: str = "",
                                   owner_email: str = "") -> bool:
    """Eskalations-Mail fuer ungenutzte Self-Service-Links (Issue #355).

    ``stage='reminder'`` (Stufe 1) -> persoenlicher Reminder an den Owner
    ``stage='oe_lead'``  (Stufe 2) -> Mail an den OE-Leiter mit Sammelhinweis

    Bewusst minimalistisch (kein eigenes Template-Setting) — Inhalt
    spiegelt Zweck und Frist (``days``).
    """
    if not recipient_email:
        return False
    if stage == "reminder":
        subject = "[IDVScope] Reminder: Sie haben offene Scanner-Funde im Self-Service"
        html = _render_email(
            accent="warning",
            kind_label="Self-Service",
            headline="Offene Scanner-Funde – bitte reagieren",
            intro_html=(
                f"<p>Hallo {recipient_name},</p>"
                f"<p>seit <strong>{days} Tagen</strong> haben Sie auf den "
                f"letzten Self-Service-Magic-Link nicht reagiert. Es liegen "
                f"noch offene Scanner-Funde zur Bewertung in Ihrem Bereich.</p>"
                f"<p>Bitte klicken Sie den Link aus der letzten "
                f"Sammelbenachrichtigung, um die Funde anzunehmen, abzulehnen "
                f"oder zur Registrierung vorzumerken.</p>"
            ),
            extra_html=(
                '<p style="font-size:12px;color:#64748b;margin-top:6px;">'
                "Nächste Eskalations-Stufe: Mail an Ihren OE-Leiter "
                "(sofern hinterlegt).</p>"
            ),
        )
    elif stage == "oe_lead":
        subject = (f"[IDVScope] Eskalation: {owner_name} hat seit {days} Tagen "
                   f"offene Scanner-Funde nicht bearbeitet")
        html = _render_email(
            accent="danger",
            kind_label="Eskalation",
            headline="Offene Scanner-Funde im Verantwortungsbereich",
            intro_html=(
                f"<p>Hallo {recipient_name},</p>"
                f"<p>als OE-Leiter werden Sie informiert, weil "
                f"<strong>{owner_name}</strong> ({owner_email}) seit "
                f"<strong>{days} Tagen</strong> nicht auf die "
                f"Self-Service-Sammelbenachrichtigung reagiert hat.</p>"
                f"<p>Bitte stoßen Sie die Bearbeitung in Ihrer OE an oder "
                f"benennen Sie eine Vertretung. Andernfalls erfolgt nach "
                f"weiteren 7 Tagen ein Eintrag im Ausnahmen-Dashboard des "
                f"IDV-Koordinators.</p>"
            ),
        )
    else:
        return False
    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_freigabe_abgeschlossen(db, idv_row, recipient_emails: list) -> bool:
    """Benachrichtigung wenn alle 4 Freigabe-Schritte erledigt wurden.

    Hängt — sofern eine ``app_base_url`` konfiguriert ist und ``idv_row``
    den Primärschlüssel ``id`` mitführt — einen CTA-Button auf die
    IDV-Detailseite an, damit der Empfänger direkt zur Doku des
    Zieldokuments springen kann.
    """
    if not _is_notify_enabled(db, "freigabe_abgeschlossen"):
        return False
    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""
    idv_db_id = _row_get(idv_row, "id")

    placeholders = {
        "idv_id":      idv_id,
        "bezeichnung": name,
    }

    subject, html, text = _load_template(
        db, "freigabe_abgeschlossen",
        _DEFAULTS["freigabe_abgeschlossen_subject"],
        _DEFAULTS["freigabe_abgeschlossen_body"],
        placeholders,
    )

    link = _idv_link(get_app_base_url(db), idv_db_id)
    if link:
        html = _inject_cta(html, "Zur IDV-Doku öffnen →", link)
        text = _strip_html_tags(html)

    return send_mail(db, recipient_emails, subject, html, text)


def get_app_base_url(db) -> str:
    """Liest die App-Basis-URL aus app_settings."""
    try:
        row = db.execute("SELECT value FROM app_settings WHERE key='app_base_url'").fetchone()
        if row and row["value"]:
            return row["value"].rstrip("/")
    except Exception:
        pass
    return ""


def notify_file_bewertung(db, file_row, recipient_email: str,
                          recipient_name: str = "") -> bool:
    """Sendet eine Bewertungsanforderung an den Datei-Ersteller/-Eigentümer.

    ``recipient_name`` (z.B. ``"Vorname Nachname"`` aus der ``persons``-
    Tabelle) wird, wenn gesetzt, in der Anrede verwendet. Sonst greift
    der bisherige Fallback auf den AD-Login aus ``file_owner`` /
    ``office_author``.
    """
    if not _is_notify_enabled(db, "bewertung"):
        return False
    if "datei_ersteller" not in set(
        get_configured_recipient_roles(db, "bewertung")
    ):
        return False
    fname = file_row["file_name"] if hasattr(file_row, "__getitem__") else str(file_row)
    fpath = file_row["full_path"] if hasattr(file_row, "__getitem__") else ""
    formula_count = file_row["formula_count"] if hasattr(file_row, "__getitem__") else 0
    has_macros = file_row["has_macros"] if hasattr(file_row, "__getitem__") else 0
    ersteller = (recipient_name or "").strip()
    if not ersteller:
        ersteller = (file_row.get("file_owner") or file_row.get("office_author") or "–") if hasattr(file_row, "__getitem__") else "–"

    placeholders = {
        "dateiname":    fname,
        "pfad":         fpath,
        "ersteller":    ersteller,
        "formelanzahl": str(formula_count or 0),
        "makros":       "Ja" if has_macros else "Nein",
    }

    subject, html, text = _load_template(
        db, "bewertung",
        _DEFAULTS["bewertung_subject"],
        _DEFAULTS["bewertung_body"],
        placeholders,
    )
    return send_mail(db, recipient_email, subject, html, text)


def notify_file_bewertung_batch(db, file_rows: list, recipient_email: str,
                                base_url: str = "",
                                recipient_name: str = "") -> bool:
    """Sendet eine kombinierte Bewertungsanforderung für mehrere Dateien an einen Empfänger.

    Fasst alle übergebenen Dateien in einer einzigen E-Mail zusammen.
    Wenn base_url angegeben ist, wird für jede Datei ein Link in IDVScope eingefügt.

    ``recipient_name`` (z.B. ``"Vorname Nachname"`` aus der ``persons``-
    Tabelle) wird, wenn gesetzt, in der Anrede verwendet. Sonst greift
    der bisherige Fallback auf den AD-Login aus ``file_owner`` /
    ``office_author``.
    """
    if not file_rows:
        return False
    if not _is_notify_enabled(db, "bewertung"):
        log.warning("Bewertungsanforderung nicht gesendet: notify_enabled_bewertung ist deaktiviert.")
        return False
    if "datei_ersteller" not in set(
        get_configured_recipient_roles(db, "bewertung")
    ):
        log.warning(
            "Bewertungsanforderung nicht gesendet: Empfaenger-Rolle "
            "'datei_ersteller' ist in den Vorlagen-Einstellungen deaktiviert."
        )
        return False

    ersteller = (recipient_name or "").strip() or "–"
    if ersteller == "–":
        for f in file_rows:
            val = (f["file_owner"] or f["office_author"] or "") if hasattr(f, "__getitem__") else ""
            if val:
                ersteller = val
                break

    n = len(file_rows)
    if n == 1:
        subject = f"[IDVScope] Bitte um Bewertung: {file_rows[0]['file_name']}"
    else:
        subject = f"[IDVScope] Bitte um Bewertung: {n} Dateien"

    with_links = bool(base_url)

    link_th = (f'<th style="padding:8px;text-align:left;color:#ffffff;">Link</th>'
               if with_links else "")
    rows_html = ""
    for i, f in enumerate(file_rows):
        bg = _BRAND["slate_50"] if i % 2 == 0 else "#ffffff"
        fname = f["file_name"] if hasattr(f, "__getitem__") else str(f)
        fpath = f["full_path"] if hasattr(f, "__getitem__") else ""
        formula_count = f["formula_count"] if hasattr(f, "__getitem__") else 0
        has_macros = f["has_macros"] if hasattr(f, "__getitem__") else 0
        file_id = f["id"] if hasattr(f, "__getitem__") else ""

        link_td = ""
        if with_links and file_id:
            link = f"{base_url}/funde?highlight={file_id}"
            link_td = (
                f'<td style="padding:8px;vertical-align:top;background:{bg};'
                f'border-bottom:1px solid {_BRAND["slate_200"]};">'
                f'<a href="{link}" style="font-size:12px;color:{_BRAND["navy_900"]};">'
                f'In IDVScope öffnen</a></td>'
            )

        rows_html += (
            f'<tr>'
            f'<td style="padding:8px;font-weight:600;vertical-align:top;'
            f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">{fname}</td>'
            f'<td style="padding:8px;font-family:monospace;font-size:11px;'
            f'vertical-align:top;background:{bg};'
            f'border-bottom:1px solid {_BRAND["slate_200"]};">{fpath}</td>'
            f'<td style="padding:8px;text-align:center;vertical-align:top;'
            f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">'
            f'{formula_count or 0}</td>'
            f'<td style="padding:8px;text-align:center;vertical-align:top;'
            f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">'
            f'{"Ja" if has_macros else "Nein"}</td>'
            f'{link_td}'
            f'</tr>'
        )

    table_html = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;width:100%;'
        f'border:1px solid {_BRAND["slate_200"]};border-radius:6px;'
        f'overflow:hidden;font-size:14px;margin:18px 0;">'
        f'<thead><tr style="background:{_BRAND["navy_900"]};color:#ffffff;">'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Dateiname</th>'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Pfad</th>'
        f'<th style="padding:8px;text-align:center;color:#ffffff;">Formeln</th>'
        f'<th style="padding:8px;text-align:center;color:#ffffff;">Makros</th>'
        f'{link_th}'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )

    scanner_link_html = ""
    if with_links:
        scanner_link_html = (
            f'<p style="margin-top:12px;">'
            f'<a href="{base_url}/funde" style="color:{_BRAND["navy_900"]};">'
            f'Zum Scanner-Eingang in IDVScope</a></p>'
        )

    html = _render_email(
        accent="info",
        kind_label="Bewertung",
        headline="Bewertung angefordert",
        intro_html=(
            f"<p>Sehr geehrte/r {ersteller},</p>"
            f"<p>die folgenden Dateien wurden vom IDVScope-Scanner erkannt "
            f"und sind Ihnen als Ersteller/Eigentümer zugeordnet. Bitte "
            f"bewerten Sie, ob diese Dateien als <strong>Individuelle "
            f"Datenverarbeitung (IDV)</strong> im Sinne von MaRisk AT 7.2 "
            f"einzustufen sind.</p>"
        ),
        extra_html=(
            f"{table_html}"
            f'<p style="margin-top:6px;">Bitte melden Sie sich in IDVScope an '
            f"und nehmen Sie die Bewertung vor.</p>"
            f"{scanner_link_html}"
        ),
    )

    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_bericht_bewertung_batch(db, bericht_rows: list, recipient_email: str,
                                   base_url: str = "",
                                   recipient_name: str = "") -> bool:
    """Sendet eine Bewertungsanforderung für Cognos-Berichte an einen Empfänger.

    ``recipient_name`` (z.B. ``"Vorname Nachname"`` aus der ``persons``-
    Tabelle) wird, wenn gesetzt, in der Anrede verwendet. Sonst greift
    der bisherige Fallback auf den ``Eigentümer``-Rohwert aus dem
    Cognos-Export.
    """
    if not bericht_rows:
        return False
    if not _is_notify_enabled(db, "bewertung"):
        log.warning("Bewertungsanforderung nicht gesendet: notify_enabled_bewertung ist deaktiviert.")
        return False
    if "datei_ersteller" not in set(
        get_configured_recipient_roles(db, "bewertung")
    ):
        log.warning(
            "Bewertungsanforderung nicht gesendet: Empfaenger-Rolle "
            "'datei_ersteller' ist in den Vorlagen-Einstellungen deaktiviert."
        )
        return False

    eigentuemer = (recipient_name or "").strip() or "–"
    if eigentuemer == "–":
        for b in bericht_rows:
            val = (b["eigentuemer"] or "") if hasattr(b, "__getitem__") else ""
            if val:
                eigentuemer = val
                break

    n = len(bericht_rows)
    if n == 1:
        subject = f"[IDVScope] Bitte um Bewertung: {bericht_rows[0]['berichtsname']}"
    else:
        subject = f"[IDVScope] Bitte um Bewertung: {n} Cognos-Berichte"

    with_links = bool(base_url)
    link_th = (f'<th style="padding:8px;text-align:left;color:#ffffff;">Link</th>'
               if with_links else "")
    rows_html = ""
    for i, b in enumerate(bericht_rows):
        bg = _BRAND["slate_50"] if i % 2 == 0 else "#ffffff"
        bname = b["berichtsname"] if hasattr(b, "__getitem__") else str(b)
        suchpfad = b["suchpfad"] if hasattr(b, "__getitem__") else ""
        bericht_id = b["id"] if hasattr(b, "__getitem__") else ""

        link_td = ""
        if with_links and bericht_id:
            link = f"{base_url}/cognos/?highlight={bericht_id}"
            link_td = (
                f'<td style="padding:8px;vertical-align:top;background:{bg};'
                f'border-bottom:1px solid {_BRAND["slate_200"]};">'
                f'<a href="{link}" style="font-size:12px;color:{_BRAND["navy_900"]};">'
                f'In IDVScope öffnen</a></td>'
            )

        rows_html += (
            f'<tr>'
            f'<td style="padding:8px;font-weight:600;vertical-align:top;'
            f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">{bname}</td>'
            f'<td style="padding:8px;font-family:monospace;font-size:11px;'
            f'vertical-align:top;background:{bg};'
            f'border-bottom:1px solid {_BRAND["slate_200"]};">{suchpfad or "–"}</td>'
            f'{link_td}'
            f'</tr>'
        )

    table_html = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;width:100%;'
        f'border:1px solid {_BRAND["slate_200"]};border-radius:6px;'
        f'overflow:hidden;font-size:14px;margin:18px 0;">'
        f'<thead><tr style="background:{_BRAND["navy_900"]};color:#ffffff;">'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Berichtsname</th>'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Suchpfad</th>'
        f'{link_th}'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )

    cognos_link_html = ""
    if with_links:
        cognos_link_html = (
            f'<p style="margin-top:12px;">'
            f'<a href="{base_url}/cognos/" style="color:{_BRAND["navy_900"]};">'
            f'Zu den Cognos-Berichten in IDVScope</a></p>'
        )

    html = _render_email(
        accent="info",
        kind_label="Bewertung",
        headline="Bewertung angefordert",
        intro_html=(
            f"<p>Sehr geehrte/r {eigentuemer},</p>"
            f"<p>die folgenden Cognos-Berichte sind Ihnen als Eigentümer "
            f"zugeordnet. Bitte bewerten Sie, ob diese Berichte als "
            f"<strong>Individuelle Datenverarbeitung (IDV)</strong> im Sinne "
            f"von MaRisk AT 7.2 einzustufen sind.</p>"
        ),
        extra_html=(
            f"{table_html}"
            f'<p style="margin-top:6px;">Bitte melden Sie sich in IDVScope an '
            f"und nehmen Sie die Bewertung vor.</p>"
            f"{cognos_link_html}"
        ),
    )

    text = _strip_html_tags(html)
    return send_mail(db, recipient_email, subject, html, text)


def notify_freigabe_pool_reminder(db, idv_row, schritt: str, pool_name: str,
                                  wartet_seit_tage: int,
                                  recipient_emails: list,
                                  action_url: str = None) -> bool:
    """Täglicher Reminder an Pool-Mitglieder für noch nicht geclaimte Schritte."""
    if not _is_notify_enabled(db, "freigabe_pool_reminder"):
        return False
    if not recipient_emails:
        return False

    idv_id = idv_row["idv_id"] if hasattr(idv_row, "__getitem__") else str(idv_row)
    name   = idv_row["bezeichnung"] if hasattr(idv_row, "__getitem__") else ""

    placeholders = {
        "idv_id":            idv_id,
        "bezeichnung":       name,
        "schritt":           schritt,
        "pool_name":         pool_name,
        "wartet_seit_tage":  str(wartet_seit_tage),
    }

    subject, html, text = _load_template(
        db, "freigabe_pool_reminder",
        _DEFAULTS["freigabe_pool_reminder_subject"],
        _DEFAULTS["freigabe_pool_reminder_body"],
        placeholders,
    )

    if action_url:
        html = _inject_cta(html, f'Schritt „{schritt}" öffnen →', action_url)
        text = _strip_html_tags(html)

    return send_mail(db, recipient_emails, subject, html, text)


def notify_owner_digest(db, recipient_email: str, recipient_name: str,
                        file_rows: list, magic_link: str,
                        base_url: str = "",
                        burst: bool = False,
                        test_banner: str | None = None) -> bool:
    """Sendet die Sammelbenachrichtigung an einen Fachbereichs-Mitarbeiter.

    ``file_rows`` sind die offenen Scanner-Funde (``bearbeitungsstatus='Neu'``)
    des Empfängers. ``magic_link`` ist die vollständige, signierte URL auf
    die Self-Service-Ansicht (Issue #315). ``burst`` = True markiert eine
    vorgezogene Sammelbenachrichtigung ausserhalb des regulären Intervalls
    (Issue #346); der Betreff wird dann mit ``[Sofort]`` gekennzeichnet.

    ``test_banner`` (Admin-Testversand): Wenn gesetzt, wird der Betreff mit
    ``[TEST]`` versehen und der Text als Banner über der Datei-Tabelle
    eingeblendet. Soll Layout-Vorschau ohne Token-Nebenwirkungen
    ermöglichen — der Aufrufer leitet die Mail in dem Fall an die
    Test-Adresse um und übergibt einen Platzhalter-Magic-Link.

    Gibt True zurück, wenn die E-Mail versendet wurde.
    """
    if not _is_notify_enabled(db, "owner_digest"):
        return False
    if not file_rows or not recipient_email or not magic_link:
        return False

    placeholders = {
        "empfaenger": recipient_name or recipient_email,
        "anzahl":     str(len(file_rows)),
        "link":       magic_link,
    }

    subject, html, text = _load_template(
        db, "owner_digest",
        _DEFAULTS["owner_digest_subject"],
        _DEFAULTS["owner_digest_body"],
        placeholders,
    )
    if burst:
        subject = f"[Sofort] {subject}"
    if test_banner:
        subject = f"[TEST] {subject}"

    # Datei-Tabelle einblenden (vor dem schließenden </body>). Die Tabelle
    # steht zusätzlich zum konfigurierbaren Body-Text, damit Admin-Anpassungen
    # am Vorwort möglich sind, die Funddaten aber immer aktuell bleiben.
    rows_html = ""
    for i, f in enumerate(file_rows):
        bg = _BRAND["slate_50"] if i % 2 == 0 else "#ffffff"
        fname = f["file_name"] if hasattr(f, "__getitem__") else str(f)
        fpath = f["full_path"] if hasattr(f, "__getitem__") else ""
        rows_html += (
            f'<tr>'
            f'<td style="padding:8px;font-weight:600;vertical-align:top;'
            f'background:{bg};border-bottom:1px solid {_BRAND["slate_200"]};">'
            f'{_html.escape(str(fname))}</td>'
            f'<td style="padding:8px;font-family:monospace;font-size:11px;'
            f'vertical-align:top;background:{bg};'
            f'border-bottom:1px solid {_BRAND["slate_200"]};">'
            f'{_html.escape(str(fpath))}</td>'
            f'</tr>'
        )
    table_html = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;width:100%;'
        f'border:1px solid {_BRAND["slate_200"]};border-radius:6px;'
        f'overflow:hidden;font-size:14px;margin:18px 0;">'
        f'<thead><tr style="background:{_BRAND["navy_900"]};color:#ffffff;">'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Dateiname</th>'
        f'<th style="padding:8px;text-align:left;color:#ffffff;">Pfad</th>'
        f'</tr></thead><tbody>{rows_html}</tbody></table>'
    )
    extra_html = table_html
    if test_banner:
        banner_html = (
            f'<div style="border:1px solid {_BRAND["amber"]};background:#fef3c7;'
            f'color:#92400e;padding:8px 12px;margin-top:12px;font-size:13px;'
            f'border-radius:4px;">'
            f'<strong>Testversand:</strong> ' + test_banner +
            '</div>'
        )
        extra_html = banner_html + table_html
    html = html.replace("</body>", extra_html + "</body>")
    text = _strip_html_tags(html)

    return send_mail(db, recipient_email, subject, html, text)


def notify_idv_incomplete(db, idv_row, score: int, missing: list,
                          recipient_emails) -> bool:
    """Erinnerung an unvollständige IDV (Schnell-Anlage, Issue #348).

    ``recipient_emails`` darf entweder ein einzelner String oder eine
    Liste sein — wird durchgereicht an ``send_mail``.
    """
    if not _is_notify_enabled(db, "idv_incomplete_reminder"):
        return False
    placeholders = {
        "idv_id":      idv_row["idv_id"],
        "bezeichnung": idv_row["bezeichnung"],
        "score":       str(int(score or 0)),
        "missing":     ", ".join(missing) if missing else "–",
    }
    subject, html, text = _load_template(
        db, "idv_incomplete_reminder",
        _DEFAULTS["idv_incomplete_reminder_subject"],
        _DEFAULTS["idv_incomplete_reminder_body"],
        placeholders,
    )

    idv_db_id = _row_get(idv_row, "idv_db_id") or _row_get(idv_row, "id")
    link = _idv_link(get_app_base_url(db), idv_db_id)
    if link:
        html = _inject_cta(html, "Zur IDV-Doku öffnen →", link)
        text = _strip_html_tags(html)

    return send_mail(db, recipient_emails, subject, html, text)


def notify_measure_overdue(db, massnahme_row,
                           recipient_emails) -> bool:
    """Eskalation für überfällige Maßnahme.

    ``recipient_emails`` darf entweder ein einzelner String oder eine
    Liste sein — wird durchgereicht an ``send_mail``.
    """
    if not _is_notify_enabled(db, "massnahme_ueberfaellig"):
        return False
    titel   = massnahme_row["titel"] if hasattr(massnahme_row, "__getitem__") else str(massnahme_row)
    faellig = massnahme_row["faellig_am"] if hasattr(massnahme_row, "__getitem__") else ""

    # Verknüpfte IDV nachschlagen, damit IDV-ID/Bezeichnung im Mail-Layout
    # erscheinen und ein direkter Link zur IDV-Doku angehängt werden kann.
    # ``massnahme_row`` enthält im Scheduler nur ``id``/``titel``/``faellig_am`` —
    # die IDV-Verknüpfung wird hier zusätzlich aufgelöst, ohne den Aufrufer
    # ändern zu müssen.
    idv_id = "–"
    bezeichnung = "–"
    idv_db_id = None
    massnahme_id = _row_get(massnahme_row, "id")
    if massnahme_id:
        try:
            ref = db.execute(
                "SELECT r.id AS idv_db_id, r.idv_id, r.bezeichnung "
                "  FROM massnahmen m "
                "  JOIN idv_register r ON r.id = m.idv_id "
                " WHERE m.id = ?",
                (massnahme_id,),
            ).fetchone()
            if ref:
                idv_id      = ref["idv_id"] or "–"
                bezeichnung = ref["bezeichnung"] or "–"
                idv_db_id   = ref["idv_db_id"]
        except Exception:
            pass

    placeholders = {
        "titel":       titel,
        "faellig_am":  faellig[:10] if faellig else "–",
        "idv_id":      idv_id,
        "bezeichnung": bezeichnung,
    }

    subject, html, text = _load_template(
        db, "massnahme_ueberfaellig",
        _DEFAULTS["massnahme_ueberfaellig_subject"],
        _DEFAULTS["massnahme_ueberfaellig_body"],
        placeholders,
    )

    link = _idv_link(get_app_base_url(db), idv_db_id)
    if link:
        html = _inject_cta(html, "Zur IDV-Doku öffnen →", link)
        text = _strip_html_tags(html)

    return send_mail(db, recipient_emails, subject, html, text)
