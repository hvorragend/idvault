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
import ssl as _ssl
from typing import Optional

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# LDAP-Konfiguration aus DB lesen
# ---------------------------------------------------------------------------

def get_ldap_config(db) -> Optional[object]:
    """Gibt die LDAP-Konfiguration zurück oder None wenn nicht vorhanden/deaktiviert."""
    try:
        return db.execute("SELECT * FROM ldap_config WHERE id = 1").fetchone()
    except Exception:
        return None


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
    """
    ad_name = person_data.get("ad_name", "")
    user_id = person_data.get("user_id", ad_name)

    # Vorhandene Person per ad_name finden
    existing = db.execute(
        "SELECT id FROM persons WHERE ad_name = ?", (ad_name,)
    ).fetchone()

    if existing:
        # Stammdaten aus LDAP aktualisieren (Rolle nur wenn aus Gruppe ermittelbar)
        update_fields = []
        params = []
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
            db.execute(
                f"UPDATE persons SET {', '.join(update_fields)} WHERE id = ?", params
            )
        db.commit()
        return existing["id"]

    # Neue Person anlegen
    kuerzel = _generate_kuerzel(db, person_data)
    rolle = person_data.get("rolle") or "Fachverantwortlicher"
    db.execute(
        """INSERT INTO persons
               (kuerzel, nachname, vorname, email, telefon, rolle, ad_name, user_id, aktiv)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            kuerzel,
            person_data.get("nachname", ""),
            person_data.get("vorname", ""),
            person_data.get("email", ""),
            person_data.get("telefon", ""),
            rolle,
            ad_name,
            user_id,
        ),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


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
        bind_pw = decrypt_password(cfg["bind_password"], secret_key)
    except Exception as e:
        logger.error("LDAP: Service-Account-Passwort konnte nicht entschlüsselt werden: %s", e)
        return None

    try:
        # TLS-Konfiguration
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

        user_attr = cfg["user_attr"] or "sAMAccountName"
        search_attrs = [
            "distinguishedName", "givenName", "sn", "mail",
            "telephoneNumber", "department", "memberOf",
            "displayName", "userAccountControl", user_attr,
        ]

        # 1. Mit Service-Account verbinden und User-DN suchen
        with Connection(
            server,
            user=cfg["bind_dn"],
            password=bind_pw,
            auto_bind=True,
            receive_timeout=15,
        ) as svc_conn:
            svc_conn.search(
                search_base=cfg["base_dn"],
                search_filter=f"({user_attr}={_escape_ldap(username)})",
                search_scope=SUBTREE,
                attributes=search_attrs,
            )
            if not svc_conn.entries:
                logger.info("LDAP: Benutzer '%s' nicht gefunden", username)
                return None

            entry = svc_conn.entries[0]
            user_dn = entry.entry_dn

            # Prüfen ob AD-Account deaktiviert (userAccountControl Bit 1)
            try:
                uac = int(str(entry.userAccountControl.value))
                if uac & 0x2:
                    logger.info("LDAP: Account '%s' ist deaktiviert (UAC=%d)", username, uac)
                    return None
            except Exception:
                pass  # UAC nicht verfügbar → weiter

            member_of = []
            try:
                member_of = [str(g) for g in entry.memberOf.values]
            except Exception:
                pass

            vorname  = _str(entry, "givenName")
            nachname = _str(entry, "sn")
            email    = _str(entry, "mail")
            telefon  = _str(entry, "telephoneNumber")

            # Fallback: displayName aufsplitten wenn givenName/sn leer
            if not vorname and not nachname:
                display = _str(entry, "displayName")
                parts = display.split(" ", 1)
                vorname  = parts[0] if parts else username
                nachname = parts[1] if len(parts) > 1 else ""

        # 2. Mit User-Credentials binden → Passwortprüfung
        with Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=True,
            receive_timeout=15,
        ):
            pass  # Erfolgreich gebunden → Passwort korrekt

        # 3. Rolle aus Gruppen ermitteln
        rolle = _get_role_from_groups(db, member_of)

        return {
            "vorname":  vorname,
            "nachname": nachname,
            "email":    email,
            "telefon":  telefon,
            "ad_name":  username,
            "user_id":  username,
            "rolle":    rolle,
        }

    except Exception as e:
        # LDAPBindError: falsches Passwort; andere: Verbindungsfehler
        logger.info("LDAP: Authentifizierung fehlgeschlagen für '%s': %s", username, e)
        return None


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
        bind_pw = decrypt_password(cfg["bind_password"], secret_key)
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
