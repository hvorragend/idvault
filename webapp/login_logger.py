"""
Login-Logger für idvscope
========================
Schreibt jeden Login-Versuch (Erfolg/Fehlschlag, Methode, IP, Grund)
in eine rotierende Datei instance/login.log.

Rotation: 2 MB pro Datei, 10 Backups → max. ~22 MB Gesamtgröße.
Zusätzlich kann die beiliegende logrotate.conf unter
/etc/logrotate.d/idvscope installiert werden (siehe Admin → Login-Log).
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_logger = logging.getLogger("idvscope.login")
_login_log_path: str = ""


def setup_login_logger(instance_path: str) -> None:
    """Einmalig aufgerufen aus create_app(). Richtet den Handler ein."""
    global _login_log_path
    if _logger.handlers:
        return  # Bereits initialisiert (z.B. durch Sidecar-Reload)

    _login_log_path = os.path.join(instance_path, "logs", "login.log")
    fh = RotatingFileHandler(
        _login_log_path,
        maxBytes=2 * 1024 * 1024,   # 2 MB pro Datei
        backupCount=10,              # login.log + .1 … .10
        encoding="utf-8",
        delay=True,                  # Datei erst anlegen wenn erster Eintrag kommt
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(fh)
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False   # nicht in idvscope.log doppelt schreiben


def get_log_path() -> str:
    return _login_log_path


def log_attempt(
    username: str,
    ip: str,
    method: str,       # "LDAP" | "lokal" | "Demo"
    success: bool,
    detail: str = "",
) -> None:
    """Schreibt einen Login-Versuch in login.log."""
    status = "OK     " if success else "FEHLER "
    parts  = [f"[{status}]", f"{method:<5}", f"{ip:>15}", repr(username)]
    if detail:
        parts.append(detail)
    msg = "  ".join(parts)
    if success:
        _logger.info(msg)
    else:
        _logger.warning(msg)


def log_ldap_step(username: str, step: str, detail: str = "", level: str = "debug") -> None:
    """Schreibt einen LDAP-Schritt (für Diagnose) in login.log."""
    msg = f"[LDAP  ]  {'':>15}  {username!r}  ↳ {step}"
    if detail:
        msg += f": {detail}"
    getattr(_logger, level)(msg)
