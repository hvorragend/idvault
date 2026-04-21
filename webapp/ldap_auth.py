"""
LDAP-Authentifizierung für idvault
====================================
Authentifiziert Benutzer per LDAP-Bind (LDAPS, Port 636) gegen ein
Active Directory über LDAPS.

Ablauf:
  1. Verbindung mit Service-Account → User-DN per sAMAccountName suchen
  2. LDAP-Bind mit User-DN + eingegebenem Passwort → Credentials prüfen
  3. memberOf-Attribute → Gruppen-Rollen-Mapping → idvault-Rolle bestimmen
  4. Person in persons-Tabelle anlegen oder aktualisieren (JIT Provisioning)
"""

import base64
import hashlib
import logging
import os
import ssl as _ssl
from typing import Optional

logger = logging.getLogger(__name__)

# Login-Logger – wird nach create_app() verfügbar; sicher importieren
def _llog(username: str, step: str, detail: str = "", level: str = "debug") -> None:
    try:
        from .login_logger import log_ldap_step
        log_ldap_step(username, step, detail, level)
    except Exception:
        pass  # Login-Logger nicht verfügbar (Tests, früher Import)

# ---------------------------------------------------------------------------
# Passwort-Verschlüsselung (Fernet, Key aus SECRET_KEY abgeleitet)
# ---------------------------------------------------------------------------

def _fernet(secret_key: str):
    from cryptography.fernet import Fernet
    raw = hashlib.sha256(secret_key.encode()).digest()
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_password(plain: str, secret_key: str) -> str:
    """Verschlüsselt ein Klartextpasswort für die Datenbank."""
    return _fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str, secret_key: str) -> str:
    """Entschlüsselt ein gespeichertes Passwort."""
    return _fernet(secret_key).decrypt(encrypted.encode()).decode()


def resolve_bind_password(cfg: dict, secret_key: str) -> str:
    """Liefert das effektive Bind-Passwort aus der DB (Fernet-entschlüsselt)."""
    raw = cfg.get("bind_password") or ""
    if not raw:
        return ""
    return decrypt_password(raw, secret_key)


# ---------------------------------------------------------------------------
# LDAP-Konfiguration aus DB lesen
# ---------------------------------------------------------------------------


def get_ldap_config(db) -> Optional[dict]:
    """Gibt die LDAP-Konfiguration aus der ``ldap_config``-Tabelle zurück.

    Liefert ``None``, wenn die Tabelle leer ist. Das Bind-Passwort bleibt
    Fernet-verschlüsselt; ``resolve_bind_password()`` entschlüsselt bei Bedarf.
    """
    try:
        row = db.execute("SELECT * FROM ldap_config WHERE id = 1").fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return dict(row)


def ldap_is_enabled(db) -> bool:
    cfg = get_ldap_config(db)
    return bool(cfg and cfg["enabled"] and cfg["server_url"])


# ---------------------------------------------------------------------------
# Gruppen → Rolle
# ---------------------------------------------------------------------------

def _get_role_from_groups(db, member_of: list) -> Optional[str]:
    """Findet die erste passende idvault-Rolle anhand der LDAP-Gruppen-DNs."""
    if not member_of:
        return None
    try:
        mappings = db.execute(
            "SELECT group_dn, rolle FROM ldap_group_role_mapping ORDER BY sort_order, id"
        ).fetchall()
    except Exception:
        return None

    member_of_lower = {g.lower() for g in member_of}
    for m in mappings:
        if m["group_dn"].lower() in member_of_lower:
            return m["rolle"]
    return None


# ---------------------------------------------------------------------------
# Person-Synchronisation (JIT Provisioning)
# ---------------------------------------------------------------------------

def ldap_sync_person(db, person_data: dict) -> Optional[int]:
    """
    Legt eine Person aus LDAP-Daten in der persons-Tabelle an oder aktualisiert sie.
    Gibt die persons.id zurück.

    Der ``db``-Parameter wird fuer Reads verwendet. Writes laufen ueber den
    zentralen Writer-Thread, damit SQLite pro Prozess nur eine Writer-
    Connection verwendet.
    """
    from .db_writer import get_writer
    from db_write_tx import write_tx

    ad_name = person_data.get("ad_name", "")

    # Vorhandene Person per ad_name finden
    existing = db.execute(
        "SELECT id FROM persons WHERE ad_name = ?", (ad_name,)
    ).fetchone()

    if existing:
        update_fields = []
        params: list = []
        for field in ("vorname", "nachname", "email", "telefon"):
            val = person_data.get(field, "").strip()
            if val:
                update_fields.append(f"{field} = ?")
                params.append(val)
        if person_data.get("rolle"):
            update_fields.append("rolle = ?")
            params.append(person_data["rolle"])
        update_fields.append("aktiv = 1")
        if update_fields:
            params.append(existing["id"])
            params_tuple = tuple(params)
            set_clause = ", ".join(update_fields)

            def _apply_update(c):
                with write_tx(c):
                    c.execute(
                        f"UPDATE persons SET {set_clause} WHERE id = ?",
                        params_tuple,
                    )
            get_writer().submit(_apply_update, wait=True)
        return existing["id"]

    rolle = person_data.get("rolle") or None
    insert_params = (
        ad_name,
        person_data.get("nachname", ""),
        person_data.get("vorname", ""),
        person_data.get("email", ""),
        person_data.get("telefon", ""),
        rolle,
        ad_name,
        ad_name,
    )

    def _apply_insert(c):
        with write_tx(c):
            cur = c.execute(
                """INSERT INTO persons
                       (kuerzel, nachname, vorname, email, telefon, rolle, ad_name, user_id, aktiv)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                insert_params,
            )
            return cur.lastrowid
    return get_writer().submit(_apply_insert, wait=True)


def _generate_kuerzel(db, person_data: dict) -> str:
    """Generiert ein eindeutiges Kürzel aus Initialen."""
    vn = (person_data.get("vorname") or "X")[:1].upper()
    nn = (person_data.get("nachname") or "LDAP")[:3].upper()
    base = f"{vn}{nn}"
    existing = {
        r["kuerzel"]
        for r in db.execute(
            "SELECT kuerzel FROM persons WHERE kuerzel LIKE ?", (f"{base}%",)
        ).fetchall()
    }
    if base not in existing:
        return base
    for i in range(2, 1000):
        candidate = f"{base}{i}"
        if candidate not in existing:
            return candidate
    return f"AD{db.execute('SELECT COUNT(*) FROM persons').fetchone()[0]}"


# ---------------------------------------------------------------------------
# Haupt-Authentifizierungsfunktion
# ---------------------------------------------------------------------------

def ldap_authenticate(db, username: str, password: str, secret_key: str) -> Optional[dict]:
    """
    Authentifiziert einen Benutzer per LDAP-Bind.

    Args:
        db:         Flask-DB-Verbindung
        username:   Eingegebener Benutzername (sAMAccountName)
        password:   Eingegebenes Passwort (Klartext, wird nur für LDAP-Bind genutzt)
        secret_key: App-SECRET_KEY zum Entschlüsseln des Service-Account-Passworts

    Returns:
        dict mit Benutzerdaten und 'rolle', oder None bei Fehlschlag.
    """
    cfg = get_ldap_config(db)
    if not cfg or not cfg["enabled"] or not cfg["server_url"]:
        return None

    if not username or not password:
        return None

    try:
        from ldap3 import Server, Connection, Tls, ALL, ALL_ATTRIBUTES, SUBTREE
        from ldap3.core.exceptions import LDAPException, LDAPBindError
    except ImportError:
        logger.error("LDAP: ldap3-Paket nicht installiert (pip install ldap3)")
        return None

    try:
        bind_pw = resolve_bind_password(cfg, secret_key)
    except Exception as e:
        msg = f"Service-Account-Passwort konnte nicht entschlüsselt werden: {e}"
        logger.error("LDAP: %s", msg)
        _llog(username, "Passwort-Entschlüsselung", msg, "error")
        return None

    _llog(username, "Konfiguration",
          f"Server={cfg['server_url']}:{cfg['port']}  SSL-Verify={cfg['ssl_verify']}  "
          f"BindDN={cfg['bind_dn']}  BaseDN={cfg['base_dn']}")

    # VULN-012: Laufzeit-Warnung bei deaktivierter Zertifikatsprüfung
    if not cfg.get("ssl_verify"):
        logger.warning(
            "LDAP-Login: Zertifikatsprüfung ist deaktiviert (ssl_verify=0). "
            "LDAPS ist damit anfällig für Man-in-the-Middle-Angriffe. "
            "Bitte ssl_verify aktivieren und internes CA-Zertifikat hinterlegen."
        )
        _llog(username, "Konfiguration",
              "WARNUNG: ssl_verify=0 – TLS-Zertifikatsprüfung deaktiviert",
              "warning")

    try:
        from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError
    except ImportError:
        LDAPBindError = LDAPSocketOpenError = Exception

    # ── Schritt 1: TLS + Server ──────────────────────────────────────────────
    try:
        if cfg["ssl_verify"]:
            tls = Tls(validate=_ssl.CERT_REQUIRED)
        else:
            tls = Tls(validate=_ssl.CERT_NONE)

        server = Server(
            cfg["server_url"],
            port=cfg["port"],
            use_ssl=True,
            tls=tls,
            get_info=ALL,
            connect_timeout=10,
        )
    except Exception as e:
        msg = f"Server-Objekt konnte nicht erstellt werden: {e}"
        logger.error("LDAP: %s", msg)
        _llog(username, "Server-Init", msg, "error")
        return None

    user_attr = cfg["user_attr"] or "sAMAccountName"
    search_attrs = [
        "distinguishedName", "givenName", "sn", "mail",
        "telephoneNumber", "department", "memberOf",
        "displayName", "userAccountControl", user_attr,
    ]

    # ── Schritt 2: Service-Account-Bind ──────────────────────────────────────
    try:
        svc_conn = Connection(
            server,
            user=cfg["bind_dn"],
            password=bind_pw,
            auto_bind=True,
            receive_timeout=15,
        )
    except LDAPBindError as e:
        msg = f"Service-Account-Bind fehlgeschlagen (falsches Passwort?): {e}"
        logger.error("LDAP: %s", msg)
        _llog(username, "Service-Bind", msg, "error")
        return None
    except Exception as e:
        msg = f"Verbindung zum LDAP-Server fehlgeschlagen: {e}"
        logger.error("LDAP: %s", msg)
        _llog(username, "Service-Bind", msg, "error")
        return None

    _llog(username, "Service-Bind", "erfolgreich")

    # ── Schritt 3: Benutzer suchen ────────────────────────────────────────────
    try:
        with svc_conn:
            svc_conn.search(
                search_base=cfg["base_dn"],
                search_filter=f"({user_attr}={_escape_ldap(username)})",
                search_scope=SUBTREE,
                attributes=search_attrs,
            )
            if not svc_conn.entries:
                msg = f"Benutzer nicht in BaseDN '{cfg['base_dn']}' gefunden (Filter: {user_attr}={username})"
                logger.warning("LDAP: %s", msg)
                _llog(username, "Benutzersuche", msg, "warning")
                return None

            entry    = svc_conn.entries[0]
            user_dn  = entry.entry_dn
            _llog(username, "Benutzersuche", f"gefunden: DN={user_dn}")

            # AD-Account deaktiviert?
            try:
                uac = int(str(entry.userAccountControl.value))
                if uac & 0x2:
                    msg = f"AD-Account ist deaktiviert (UAC={uac})"
                    logger.warning("LDAP: '%s' – %s", username, msg)
                    _llog(username, "Account-Status", msg, "warning")
                    return None
                _llog(username, "Account-Status", f"aktiv (UAC={uac})")
            except Exception:
                _llog(username, "Account-Status", "UAC nicht lesbar – wird ignoriert")

            member_of = []
            try:
                member_of = [str(g) for g in entry.memberOf.values]
                _llog(username, "Gruppenmitgliedschaft",
                      f"{len(member_of)} Gruppe(n): {', '.join(member_of[:5])}"
                      + (" …" if len(member_of) > 5 else ""))
            except Exception:
                _llog(username, "Gruppenmitgliedschaft", "memberOf nicht lesbar")

            vorname  = _str(entry, "givenName")
            nachname = _str(entry, "sn")
            email    = _str(entry, "mail")
            telefon  = _str(entry, "telephoneNumber")

            if not vorname and not nachname:
                display  = _str(entry, "displayName")
                parts    = display.split(" ", 1)
                vorname  = parts[0] if parts else username
                nachname = parts[1] if len(parts) > 1 else ""

    except Exception as e:
        msg = f"Fehler während der Benutzersuche: {e}"
        logger.error("LDAP: %s", msg)
        _llog(username, "Benutzersuche", msg, "error")
        return None

    # ── Schritt 4: Benutzer-Bind (Passwort prüfen) ───────────────────────────
    try:
        with Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=True,
            receive_timeout=15,
        ):
            pass  # Bind erfolgreich → Passwort korrekt
        _llog(username, "Passwort-Prüfung", "erfolgreich")
    except LDAPBindError as e:
        msg = f"Falsches Passwort: {e}"
        logger.info("LDAP: '%s' – %s", username, msg)
        _llog(username, "Passwort-Prüfung", msg, "warning")
        return None
    except Exception as e:
        msg = f"Fehler beim Benutzer-Bind: {e}"
        logger.error("LDAP: '%s' – %s", username, msg)
        _llog(username, "Passwort-Prüfung", msg, "error")
        return None

    # ── Schritt 5: Rolle ermitteln ────────────────────────────────────────────
    rolle = _get_role_from_groups(db, member_of)
    if rolle:
        _llog(username, "Rollen-Mapping", f"Gruppe → '{rolle}'")
    else:
        # Fallback: gespeicherte Rolle aus DB
        try:
            existing = db.execute(
                "SELECT rolle FROM persons WHERE ad_name = ? AND aktiv = 1",
                (username,)
            ).fetchone()
            if existing and existing["rolle"]:
                rolle = existing["rolle"]
                _llog(username, "Rollen-Mapping",
                      f"kein Gruppen-Mapping – DB-Fallback: '{rolle}'", "info")
                logger.info(
                    "LDAP: Keine Gruppen-Rollenzuordnung für '%s' – verwende gespeicherte Rolle '%s'",
                    username, rolle,
                )
            else:
                _llog(username, "Rollen-Mapping", "keine Rolle ermittelbar (kein Mapping, kein DB-Eintrag)", "warning")
        except Exception:
            pass

    return {
        "vorname":  vorname,
        "nachname": nachname,
        "email":    email,
        "telefon":  telefon,
        "ad_name":  username,
        "user_id":  username,
        "rolle":    rolle,
    }


# ---------------------------------------------------------------------------
# LDAP-Benutzer auflisten (für Admin-Import)
# ---------------------------------------------------------------------------

def ldap_list_users(db, secret_key: str, extra_filter: str = "") -> tuple[bool, str, list]:
    """
    Listet alle aktivierten AD-Benutzer auf.

    Args:
        db:           DB-Verbindung (für Konfiguration und Rollen-Mapping)
        secret_key:   App-SECRET_KEY zum Entschlüsseln des Service-Account-Passworts
        extra_filter: Optionaler LDAP-Suchfilter (z. B. "(department=IT)")

    Returns:
        (success: bool, message: str, users: list[dict])
        Jedes user-dict enthält: vorname, nachname, email, telefon, user_id, ad_name, rolle
    """
    try:
        from ldap3 import Server, Connection, Tls, ALL, SUBTREE
    except ImportError:
        return False, "ldap3-Paket nicht installiert (pip install ldap3)", []

    cfg = get_ldap_config(db)
    if not cfg or not cfg["server_url"]:
        return False, "Keine LDAP-Konfiguration vorhanden.", []

    try:
        bind_pw = resolve_bind_password(cfg, secret_key)
    except Exception:
        return False, "Service-Account-Passwort konnte nicht entschlüsselt werden.", []

    # Nur aktivierte Benutzerkonten (userAccountControl-Bit 0x2 = deaktiviert)
    base_filter = "(&(objectClass=user)(!(objectClass=computer))(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
    if extra_filter.strip():
        search_filter = f"(&{base_filter}{extra_filter.strip()})"
    else:
        search_filter = base_filter

    user_attr = cfg["user_attr"] or "sAMAccountName"
    search_attrs = [
        "distinguishedName", "givenName", "sn", "mail",
        "telephoneNumber", "department", "memberOf",
        "displayName", user_attr,
    ]

    try:
        import ssl as _ssl
        tls = Tls(validate=_ssl.CERT_NONE if not cfg.get("ssl_verify", 1) else _ssl.CERT_REQUIRED)
        server = Server(
            cfg["server_url"], port=cfg["port"],
            use_ssl=True, tls=tls, get_info=ALL, connect_timeout=10,
        )
        with Connection(server, user=cfg["bind_dn"], password=bind_pw,
                        auto_bind=True, receive_timeout=30) as conn:
            conn.search(
                search_base=cfg["base_dn"],
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=search_attrs,
                paged_size=500,
            )
            entries = conn.entries

    except Exception as e:
        return False, f"LDAP-Verbindungsfehler: {e}", []

    users = []
    for entry in entries:
        vorname  = _str(entry, "givenName")
        nachname = _str(entry, "sn")
        if not vorname and not nachname:
            display = _str(entry, "displayName")
            parts = display.split(" ", 1)
            vorname  = parts[0] if parts else ""
            nachname = parts[1] if len(parts) > 1 else ""

        member_of = []
        try:
            member_of = [str(g) for g in entry.memberOf.values]
        except Exception:
            pass

        uid = _str(entry, user_attr)
        if not uid:
            continue  # Benutzer ohne Anmeldename überspringen

        rolle = _get_role_from_groups(db, member_of)
        users.append({
            "vorname":   vorname,
            "nachname":  nachname,
            "email":     _str(entry, "mail"),
            "telefon":   _str(entry, "telephoneNumber"),
            "abteilung": _str(entry, "department"),
            "user_id":   uid,
            "ad_name":   uid,
            "rolle":     rolle or "",
            "member_of": member_of,
        })

    users.sort(key=lambda u: (u["nachname"].lower(), u["vorname"].lower()))
    return True, f"{len(users)} Benutzer gefunden.", users


# ---------------------------------------------------------------------------
# LDAP-Verbindungstest (für Admin-UI)
# ---------------------------------------------------------------------------

def ldap_test_connection(cfg: dict, secret_key: str) -> tuple[bool, str]:
    """
    Testet die LDAP-Verbindung mit dem Service-Account.

    Returns:
        (success: bool, message: str)
    """
    try:
        from ldap3 import Server, Connection, Tls, ALL, SUBTREE
    except ImportError:
        return False, "ldap3-Paket nicht installiert (pip install ldap3)"

    try:
        bind_pw = resolve_bind_password(cfg, secret_key)
    except Exception:
        return False, "Service-Account-Passwort konnte nicht entschlüsselt werden."

    try:
        tls = Tls(validate=_ssl.CERT_NONE if not cfg.get("ssl_verify", True) else _ssl.CERT_REQUIRED)
        server = Server(
            cfg["server_url"], port=cfg["port"],
            use_ssl=True, tls=tls, get_info=ALL, connect_timeout=10
        )
        with Connection(server, user=cfg["bind_dn"], password=bind_pw,
                        auto_bind=True, receive_timeout=15) as conn:
            # Kurze Suche um Base-DN zu validieren
            conn.search(cfg["base_dn"], "(objectClass=*)", search_scope="BASE",
                        attributes=["objectClass"])
            return True, f"Verbindung erfolgreich. Server: {server.host}:{server.port}"
    except Exception as e:
        return False, f"Verbindungsfehler: {e}"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _str(entry, attr: str) -> str:
    """Liest ein LDAP-Attribut sicher als String aus."""
    try:
        val = getattr(entry, attr).value
        return str(val).strip() if val else ""
    except Exception:
        return ""


def _escape_ldap(value: str) -> str:
    """Escapet Sonderzeichen in LDAP-Suchwerten (RFC 4515)."""
    escaped = (
        value
        .replace("\\", "\\5c")
        .replace("*",  "\\2a")
        .replace("(",  "\\28")
        .replace(")",  "\\29")
        .replace("\0", "\\00")
    )
    return escaped
