"""
IDV Teams-Scanner – Microsoft Teams / SharePoint Online Discovery via Graph API
================================================================================
Scannt Teams-Kanäle und SharePoint-Dokumentbibliotheken nach IDV-Dateien und
speichert Metadaten in derselben SQLite-Datenbank wie der idv_scanner.py.

Voraussetzungen:
    pip install msal requests

Azure AD App-Registrierung (einmalig durch IT-Administrator):
    1. Azure Portal → Entra ID → App-Registrierungen → Neue Registrierung
    2. API-Berechtigungen → Microsoft Graph → Anwendungsberechtigungen:
           Files.Read.All    (Dateien in allen Sites lesen)
           Sites.Read.All    (Site-Metadaten lesen)
    3. Admin-Zustimmung erteilen
    4. Zertifikate & Geheimnisse → Neuer geheimer Clientschlüssel

Konfiguration:
    Wird aus der SQLite-Datenbank (``app_settings``) gelesen. Die Webapp
    pflegt Tenant-/Client-ID, client_secret (Fernet-verschlüsselt) und
    Teams-/Site-Liste über ``/admin/teams-einstellungen``. Der Scanner
    wird vom Webapp-Prozess als Subprocess mit ``--db-path <idvault.db>``
    aufgerufen.

Autor:  IDV-Register Projekt
Lizenz: intern
"""

import os
import sys
import json
import time
import sqlite3
import logging
import argparse
import tempfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Shared utilities aus idv_scanner importieren
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from idv_scanner import (
    analyze_ooxml,
    upsert_file,
    mark_deleted_files,
    setup_logging,
    init_db,
)
from path_utils import apply_path_mappings, should_pass_filters

# ---------------------------------------------------------------------------
# Optionale Abhängigkeiten
# ---------------------------------------------------------------------------
try:
    import msal
    HAS_MSAL = True
except ImportError:
    HAS_MSAL = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

OOXML_EXTENSIONS = {
    ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
    ".docx", ".docm", ".dotm",
    ".pptx", ".pptm",
}

DEFAULT_EXTENSIONS = [
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
    ".accdb", ".mdb", ".accde", ".accdr",
    ".ida", ".idv",
    ".pbix", ".pbit",
    ".dotm", ".pptm",
    ".py", ".r", ".rmd",
    ".sql",
]

DEFAULT_CONFIG = {
    "tenant_id":           "",
    "client_id":           "",
    # Bei --db-path-Modus wird das client_secret Fernet-verschlüsselt aus
    # app_settings['teams_client_secret_enc'] geladen.
    "client_secret":       "",
    "extensions":          DEFAULT_EXTENSIONS,
    # Dateien > X MB werden nicht heruntergeladen (OOXML-Analyse übersprungen)
    "hash_size_limit_mb":  100,
    # True: Dateien für Makro-/Formel-Erkennung herunterladen (empfohlen)
    # False: Nur Graph-Metadaten, keine OOXML-Analyse
    "download_for_ooxml":  True,
    "move_detection":      "name_and_hash",
    "teams": [
        # Beispiele (auskommentiert):
        # { "team_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "display_name": "IDV-Team" },
        # { "site_url": "https://contoso.sharepoint.com/sites/Controlling", "display_name": "Controlling" }
    ],
}

# ---------------------------------------------------------------------------
# Datenbank-Migration
# ---------------------------------------------------------------------------

_MIGRATION_STMTS = [
    # source: Herkunft der Datei ('filesystem' | 'sharepoint' | 'teams')
    "ALTER TABLE idv_files ADD COLUMN source TEXT DEFAULT 'filesystem'",
    # Stabiler SharePoint-Item-ID (bleibt bei Umbenennung / Verschieben erhalten)
    "ALTER TABLE idv_files ADD COLUMN sharepoint_item_id TEXT",
    # Delta-Token-Speicher für inkrementellen Graph-API-Sync pro Drive
    """CREATE TABLE IF NOT EXISTS teams_delta_tokens (
        drive_id    TEXT PRIMARY KEY,
        delta_token TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_files_sp_item ON idv_files(sharepoint_item_id)",
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Fügt fehlende Spalten / Tabellen hinzu (idempotent, safe to run multiple times)."""
    for stmt in _MIGRATION_STMTS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                pass  # Spalte / Tabelle / Index existiert bereits – OK
            else:
                raise


# ---------------------------------------------------------------------------
# Authentifizierung (MSAL Client-Credentials-Flow)
# ---------------------------------------------------------------------------

def get_access_token(config: dict, logger: logging.Logger) -> str:
    """Holt ein Access Token via MSAL Client-Credentials-Flow (keine Benutzeranmeldung)."""
    if not HAS_MSAL:
        logger.error("MSAL nicht installiert. Bitte: pip install msal")
        sys.exit(1)

    tenant_id     = config.get("tenant_id", "")
    client_id     = config.get("client_id", "")
    client_secret = config.get("client_secret", "")

    if not all([tenant_id, client_id, client_secret]):
        logger.error(
            "Konfiguration unvollständig: tenant_id, client_id und "
            "client_secret müssen gesetzt sein."
        )
        sys.exit(1)

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "Unbekannter Fehler"
        logger.error(f"Token-Anfrage fehlgeschlagen: {err}")
        sys.exit(1)

    logger.debug("Access Token erfolgreich abgerufen.")
    return result["access_token"]


# ---------------------------------------------------------------------------
# Graph API HTTP-Client
# ---------------------------------------------------------------------------

class GraphClient:
    """Schlanker HTTP-Client für die Microsoft Graph API mit Retry-Logik."""

    def __init__(self, token: str, logger: logging.Logger):
        if not HAS_REQUESTS:
            logger.error("requests nicht installiert. Bitte: pip install requests")
            sys.exit(1)
        self._session = _requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        })
        self.logger = logger

    def get(self, url: str, params: dict = None, max_retries: int = 4) -> dict:
        """GET-Anfrage mit exponentiellem Backoff bei 429 (Throttling) und 503."""
        delay = 2
        for attempt in range(max_retries + 1):
            resp = self._session.get(url, params=params, timeout=30)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in (429, 503):
                retry_after = int(resp.headers.get("Retry-After", delay))
                self.logger.warning(
                    f"Graph API gedrosselt (HTTP {resp.status_code}), "
                    f"warte {retry_after}s (Versuch {attempt + 1}/{max_retries})"
                )
                time.sleep(retry_after)
                delay = min(delay * 2, 60)
                continue

            resp.raise_for_status()

        raise RuntimeError(
            f"Graph API-Fehler nach {max_retries} Versuchen: {url}"
        )

    def download_bytes(self, drive_id: str, item_id: str,
                       max_bytes: Optional[int] = None) -> Optional[bytes]:
        """Lädt Dateiinhalt über den /content-Endpunkt herunter."""
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        headers = {}
        if max_bytes:
            headers["Range"] = f"bytes=0-{max_bytes - 1}"
        try:
            resp = self._session.get(url, headers=headers, timeout=60,
                                     allow_redirects=True)
            if resp.status_code in (200, 206):
                return resp.content
            self.logger.debug(
                f"Download HTTP {resp.status_code} für item {item_id}"
            )
        except Exception as exc:
            self.logger.debug(f"Download-Fehler für item {item_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Delta-Token-Verwaltung
# ---------------------------------------------------------------------------

def load_delta_token(conn: sqlite3.Connection, drive_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT delta_token FROM teams_delta_tokens WHERE drive_id = ?",
        (drive_id,),
    ).fetchone()
    return row["delta_token"] if row else None


def save_delta_token(conn: sqlite3.Connection, drive_id: str,
                     token: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO teams_delta_tokens (drive_id, delta_token, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE
            SET delta_token = excluded.delta_token,
                updated_at  = excluded.updated_at
        """,
        (drive_id, token, now),
    )


# ---------------------------------------------------------------------------
# Drive-Ermittlung (Team-ID oder Site-URL → Drive-ID)
# ---------------------------------------------------------------------------

def resolve_drive(
    client: GraphClient, team_entry: dict, logger: logging.Logger
) -> Optional[tuple]:
    """
    Ermittelt (drive_id, site_url, display_name) für einen Teams/SharePoint-Eintrag.

    Akzeptiert:
        { "team_id":  "...", "display_name": "..." }
        { "site_url": "https://contoso.sharepoint.com/sites/...", "display_name": "..." }
    """
    display_name = team_entry.get("display_name", "")

    # ── Variante A: Microsoft Teams-Team ────────────────────────────────────
    if "team_id" in team_entry:
        team_id = team_entry["team_id"]
        try:
            data     = client.get(f"{GRAPH_BASE}/groups/{team_id}/drive")
            drive_id = data["id"]
            web_url  = data.get("webUrl", "")
            # webUrl: "https://contoso.sharepoint.com/sites/TeamName/Shared%20Documents"
            # site_url: alles vor dem letzten Bibliothek-Segment
            site_url = web_url
            for marker in ("/Shared%20Documents", "/Shared Documents",
                           "/Documents", "/Freigegebene%20Dokumente",
                           "/Freigegebene Dokumente"):
                if marker.lower() in web_url.lower():
                    site_url = web_url[: web_url.lower().index(marker.lower())]
                    break
            display_name = display_name or data.get("name", team_id)
            logger.info(f"Team '{display_name}' → Drive-ID: {drive_id[:8]}…")
            return drive_id, site_url, display_name
        except Exception as exc:
            logger.error(f"Drive-Auflösung fehlgeschlagen für Team {team_id}: {exc}")
            return None

    # ── Variante B: SharePoint-Site-URL ─────────────────────────────────────
    if "site_url" in team_entry:
        site_url = team_entry["site_url"].rstrip("/")
        try:
            parsed   = urlparse(site_url)
            hostname = parsed.netloc
            path     = parsed.path
            site_data = client.get(f"{GRAPH_BASE}/sites/{hostname}:{path}")
            site_id   = site_data["id"]

            drives_data = client.get(f"{GRAPH_BASE}/sites/{site_id}/drives")
            drives      = drives_data.get("value", [])

            # Standardbibliothek finden: "Freigegebene Dokumente" / "Documents"
            doc_drive = None
            for d in drives:
                if d.get("driveType") != "documentLibrary":
                    continue
                name_lower = d.get("name", "").lower()
                if any(kw in name_lower for kw in
                       ("shared", "freigegebene", "documents", "dokumente")):
                    doc_drive = d
                    break
            if not doc_drive and drives:
                doc_drive = drives[0]  # Fallback: erste verfügbare Bibliothek

            if not doc_drive:
                logger.error(f"Keine Dokumentbibliothek gefunden für: {site_url}")
                return None

            drive_id     = doc_drive["id"]
            display_name = display_name or site_data.get("displayName", site_url)
            logger.info(f"Site '{display_name}' → Drive-ID: {drive_id[:8]}…")
            return drive_id, site_url, display_name

        except Exception as exc:
            logger.error(f"Drive-Auflösung fehlgeschlagen für Site {site_url}: {exc}")
            return None

    logger.error(
        f"Ungültiger Teams-Eintrag: weder 'team_id' noch 'site_url' vorhanden: "
        f"{team_entry}"
    )
    return None


# ---------------------------------------------------------------------------
# Hilfsfunktion: gelöschtes SharePoint-Item archivieren (Inkremental-Scan)
# ---------------------------------------------------------------------------

def _archive_deleted_item(
    conn: sqlite3.Connection, item: dict,
    scan_run_id: int, now: str, logger: logging.Logger
) -> None:
    """Archiviert eine via Delta-Query als gelöscht gemeldete Datei in der DB."""
    item_id = item.get("id")
    if not item_id:
        return
    row = conn.execute(
        "SELECT id, full_path FROM idv_files "
        "WHERE sharepoint_item_id = ? AND status = 'active'",
        (item_id,),
    ).fetchone()
    if not row:
        return
    file_id = row["id"]
    conn.execute(
        "UPDATE idv_files SET status = 'archiviert', last_seen_at = ? WHERE id = ?",
        (now, file_id),
    )
    conn.execute(
        """
        INSERT INTO idv_file_history (file_id, scan_run_id, change_type, changed_at)
        VALUES (?, ?, 'archiviert', ?)
        """,
        (file_id, scan_run_id, now),
    )
    logger.debug(f"SharePoint-Datei als gelöscht markiert: {row['full_path']}")


# ---------------------------------------------------------------------------
# Metadaten aus einem Graph-DriveItem aufbauen
# ---------------------------------------------------------------------------

def build_file_metadata(
    item: dict, drive_id: str, site_url: str,
    client: GraphClient, config: dict, logger: logging.Logger,
) -> Optional[dict]:
    """
    Wandelt ein Graph-API-DriveItem in ein idv_files-kompatibles Metadaten-Dict um.
    Lädt die Datei bei Bedarf für OOXML-Analyse (Makros, Formeln, Blattschutz) herunter.
    Gibt None zurück, wenn die Datei keine konfigurierte Erweiterung hat.
    """
    name = item.get("name", "")
    ext  = Path(name).suffix.lower()

    extensions = {e.lower() for e in config.get("extensions", DEFAULT_EXTENSIONS)}
    if ext not in extensions:
        return None

    item_id          = item.get("id", "")
    web_url          = item.get("webUrl", "")
    size_bytes       = item.get("size", 0) or 0
    created_at       = item.get("createdDateTime")
    modified_at      = item.get("lastModifiedDateTime")
    created_by_name  = (
        (item.get("createdBy")      or {})
        .get("user", {}) or {}
    ).get("displayName")
    modified_by_name = (
        (item.get("lastModifiedBy") or {})
        .get("user", {}) or {}
    ).get("displayName")

    # Relativer Pfad: parentReference.path = "/drives/{id}/root:/Ordner/Unterordner"
    parent_path = (item.get("parentReference") or {}).get("path", "")
    if "root:" in parent_path:
        rel_folder = parent_path.split("root:", 1)[1].lstrip("/")
    else:
        rel_folder = ""
    relative_path = f"{rel_folder}/{name}" if rel_folder else name

    # SHA-256 aus Graph-Metadaten (wenn vorhanden)
    sha256_from_graph = (
        (item.get("file") or {})
        .get("hashes", {}) or {}
    ).get("sha256Hash", "").lower() or None

    file_hash    = sha256_from_graph or "HASH_ERROR"
    ooxml_result = {}

    # OOXML-Analyse via Download (nur für Office-Dateien unterhalb des Größenlimits)
    hash_limit_bytes = config.get("hash_size_limit_mb", 100) * 1024 * 1024
    if (
        ext in OOXML_EXTENSIONS
        and config.get("download_for_ooxml", True)
        and size_bytes <= hash_limit_bytes
        and item_id
    ):
        data_bytes = client.download_bytes(drive_id, item_id)
        if data_bytes:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=ext, delete=False
                ) as tmp:
                    tmp.write(data_bytes)
                    tmp_path = tmp.name
                # Hash aus Dateiinhalt (falls Graph keinen SHA-256 geliefert hat)
                if not sha256_from_graph:
                    file_hash = hashlib.sha256(data_bytes).hexdigest()
                ooxml_result = analyze_ooxml(tmp_path, ext)
            except Exception as exc:
                logger.debug(f"OOXML-Analyse fehlgeschlagen für '{name}': {exc}")
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    mappings = config.get("path_mappings", [])
    stored_full_path  = apply_path_mappings(web_url,  mappings)
    stored_share_root = apply_path_mappings(site_url, mappings)

    return {
        # Pflichtfelder idv_files
        "file_hash":              file_hash,
        "full_path":              stored_full_path,
        "file_name":              name,
        "extension":              ext,
        "share_root":             stored_share_root,
        "relative_path":          relative_path,
        "size_bytes":             size_bytes,
        "created_at":             created_at,
        "modified_at":            modified_at,
        "file_owner":             modified_by_name,
        "office_author":          created_by_name,
        "office_last_author":     modified_by_name,
        "office_created":         created_at,
        "office_modified":        modified_at,
        # OOXML-Felder (0 als sicherer Fallback)
        "has_macros":             ooxml_result.get("has_macros", 0),
        "has_external_links":     ooxml_result.get("has_external_links", 0),
        "sheet_count":            ooxml_result.get("sheet_count"),
        "named_ranges_count":     ooxml_result.get("named_ranges_count"),
        "formula_count":          ooxml_result.get("formula_count", 0),
        "has_sheet_protection":   ooxml_result.get("has_sheet_protection", 0),
        "protected_sheets_count": ooxml_result.get("protected_sheets_count", 0),
        "sheet_protection_has_pw":ooxml_result.get("sheet_protection_has_pw", 0),
        "workbook_protected":     ooxml_result.get("workbook_protected", 0),
        # Interne Felder (werden nach upsert_file gesetzt, nicht direkt eingefügt)
        "_source":                "sharepoint",
        "_sharepoint_item_id":    item_id,
    }


# ---------------------------------------------------------------------------
# Kern: eine Drive via Delta-Query scannen
# ---------------------------------------------------------------------------

def scan_drive(
    conn: sqlite3.Connection,
    client: GraphClient,
    drive_id: str,
    site_url: str,
    display_name: str,
    scan_run_id: int,
    now: str,
    config: dict,
    logger: logging.Logger,
) -> tuple:
    """
    Scannt eine SharePoint-Drive via Graph API Delta-Query.

    Gibt zurück: (stats_dict, was_full_scan)
    - was_full_scan = True  → alle Dateien wurden gelesen (kein Delta-Token vorhanden)
    - was_full_scan = False → nur Änderungen seit letztem Scan (Delta-Token genutzt)
    """
    stats = {
        "total": 0, "new": 0, "changed": 0, "unchanged": 0,
        "moved": 0, "restored": 0, "errors": 0,
    }
    move_mode = config.get("move_detection", "name_and_hash")

    # Delta-Token laden → inkrementeller oder vollständiger Scan?
    saved_token  = load_delta_token(conn, drive_id)
    was_full_scan = saved_token is None

    if saved_token:
        # Das savedToken IST bereits die vollständige deltaLink-URL
        initial_url    = saved_token
        initial_params = None
        logger.info(f"[{display_name}] Inkrementeller Scan (Delta-Token vorhanden)")
    else:
        initial_url    = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
        initial_params = {
            "$select": (
                "id,name,size,webUrl,file,deleted,"
                "createdBy,lastModifiedBy,"
                "createdDateTime,lastModifiedDateTime,"
                "parentReference"
            )
        }
        logger.info(f"[{display_name}] Vollständiger Erstscan")

    # Delta-Query mit Paginierung
    next_url    = initial_url
    params      = initial_params
    new_delta_token = None

    while next_url:
        try:
            page = client.get(next_url, params=params)
        except Exception as exc:
            logger.error(f"[{display_name}] Graph-API-Fehler: {exc}")
            stats["errors"] += 1
            break

        # Nach dem ersten Request keine Params mehr übergeben (nextLink enthält sie bereits)
        params = None

        for item in page.get("value", []):
            # Gelöschte Einträge im Inkremental-Scan explizit archivieren
            if "deleted" in item:
                if not was_full_scan:
                    _archive_deleted_item(conn, item, scan_run_id, now, logger)
                continue
            # Ordner überspringen
            if "file" not in item:
                continue

            try:
                meta = build_file_metadata(
                    item, drive_id, site_url, client, config, logger
                )
                if meta is None:
                    continue

                # Blacklist/Whitelist gegen relative_path prüfen
                blacklist = config.get("blacklist_paths", [])
                whitelist = config.get("whitelist_paths", [])
                if not should_pass_filters(meta["relative_path"], blacklist, whitelist):
                    logger.debug(f"Übersprungen (Filter): {meta['relative_path']}")
                    continue

                # Interne Felder extrahieren (nicht Teil des Standard-Schemas)
                source      = meta.pop("_source", "sharepoint")
                sp_item_id  = meta.pop("_sharepoint_item_id", None)

                change = upsert_file(conn, meta, scan_run_id, now, logger, move_mode)
                stats["total"] += 1
                stats[change]  += 1

                # source und sharepoint_item_id nachtragen
                conn.execute(
                    "UPDATE idv_files SET source = ?, sharepoint_item_id = ? "
                    "WHERE full_path = ?",
                    (source, sp_item_id, meta["full_path"]),
                )

                if stats["total"] % 50 == 0:
                    conn.commit()
                    logger.info(
                        f"  [{display_name}] … {stats['total']} Dateien verarbeitet"
                    )

            except Exception as exc:
                logger.error(
                    f"[{display_name}] Fehler bei '{item.get('name', '?')}': {exc}"
                )
                stats["errors"] += 1

        # nextLink weiterverfolgen, deltaLink am Ende speichern
        if "@odata.deltaLink" in page:
            new_delta_token = page["@odata.deltaLink"]
        next_url = page.get("@odata.nextLink")

    conn.commit()

    # Neuen Delta-Token für nächsten inkrementellen Scan speichern
    if new_delta_token:
        save_delta_token(conn, drive_id, new_delta_token, now)
        conn.commit()
        logger.debug(f"[{display_name}] Neuer Delta-Token gespeichert.")

    logger.info(
        f"[{display_name}] Drive-Scan abgeschlossen – "
        f"{stats['total']} Dateien, {stats['new']} neu, "
        f"{stats['changed']} geändert, {stats['errors']} Fehler"
    )
    return stats, was_full_scan


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def run_teams_scan(config: dict, logger: logging.Logger) -> None:
    teams = config.get("teams", [])
    if not teams:
        logger.error(
            "Keine Teams/Sites konfiguriert. "
            "Bitte teams_config.json anpassen (Schlüssel: 'teams')."
        )
        sys.exit(1)

    conn = init_db(config["db_path"])
    apply_migrations(conn)
    now  = datetime.now(timezone.utc).isoformat()

    # Scan-Run protokollieren
    labels = [
        t.get("display_name") or t.get("team_id") or t.get("site_url", "?")
        for t in teams
    ]
    cur = conn.execute(
        "INSERT INTO scan_runs (started_at, scan_paths) VALUES (?, ?)",
        (now, json.dumps(labels)),
    )
    conn.commit()
    scan_run_id = cur.lastrowid
    logger.info(f"Teams-Scan-Run #{scan_run_id} gestartet | Quellen: {labels}")

    token  = get_access_token(config, logger)
    client = GraphClient(token, logger)

    total_stats = {
        "total": 0, "new": 0, "changed": 0, "unchanged": 0,
        "moved": 0, "restored": 0, "errors": 0,
    }
    full_scan_site_urls = []  # Nur für vollständige Scans → mark_deleted_files

    for team_entry in teams:
        result = resolve_drive(client, team_entry, logger)
        if not result:
            total_stats["errors"] += 1
            continue

        drive_id, site_url, display_name = result
        stats, was_full_scan = scan_drive(
            conn, client, drive_id, site_url, display_name,
            scan_run_id, now, config, logger,
        )
        for key in total_stats:
            total_stats[key] += stats.get(key, 0)

        if was_full_scan:
            full_scan_site_urls.append(site_url)

    conn.commit()

    # Dateien, die im Vollscan nicht mehr gesehen wurden, archivieren.
    # Bei Inkremental-Scans werden Löschungen bereits über den Delta-Response behandelt.
    deleted = 0
    if full_scan_site_urls:
        deleted = mark_deleted_files(
            conn, scan_run_id, now, scan_paths=full_scan_site_urls
        )
        conn.commit()

    # Scan-Run abschließen
    finished = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE scan_runs SET
            finished_at    = ?,
            total_files    = ?,
            new_files      = ?,
            changed_files  = ?,
            moved_files    = ?,
            restored_files = ?,
            archived_files = ?,
            errors         = ?
        WHERE id = ?
        """,
        (
            finished,
            total_stats["total"],
            total_stats["new"],
            total_stats["changed"],
            total_stats["moved"],
            total_stats["restored"],
            deleted,
            total_stats["errors"],
            scan_run_id,
        ),
    )
    conn.commit()
    conn.close()

    logger.info("=" * 60)
    logger.info(f"Teams-Scan abgeschlossen in Run #{scan_run_id}")
    logger.info(f"  Gesamt gefunden : {total_stats['total']}")
    logger.info(f"  Neu             : {total_stats['new']}")
    logger.info(f"  Geändert        : {total_stats['changed']}")
    logger.info(f"  Verschoben      : {total_stats['moved']}")
    logger.info(f"  Wiederhergest.  : {total_stats['restored']}")
    logger.info(f"  Archiviert      : {deleted}")
    logger.info(f"  Fehler          : {total_stats['errors']}")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_check(ok: bool, label: str) -> None:
    mark = "OK" if ok else "FEHLT"
    print(f"  [{mark}] {label}")


def _load_teams_config_from_db(db_path: str, secret_key: str) -> dict:
    """Lädt teams_config + Fernet-entschlüsseltes client_secret aus app_settings."""
    import sqlite3
    cfg = dict(DEFAULT_CONFIG)
    cfg["db_path"] = db_path
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        def _read(key: str) -> str:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
            return (row["value"] if row and row["value"] else "") or ""

        raw = _read("teams_config")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    cfg.update(data)
            except (TypeError, ValueError):
                pass

        enc = _read("teams_client_secret_enc")
        if enc and secret_key:
            try:
                # secrets.py liegt in webapp/; Scanner importiert es über
                # project-root (Pfad wird unten in main() gesetzt).
                from webapp.secrets import decrypt_with
                cfg["client_secret"] = decrypt_with(secret_key, enc)
            except Exception:
                cfg["client_secret"] = ""

        raw_pm = _read("path_mappings")
        if raw_pm:
            try:
                pm = json.loads(raw_pm)
                if isinstance(pm, list):
                    cfg["path_mappings"] = pm
            except (TypeError, ValueError):
                pass
    finally:
        conn.close()
    return cfg


def _load_bootstrap_secret_key() -> str:
    """Liest SECRET_KEY aus config.json (für Subprozesse ohne Flask-App-Kontext)."""
    try:
        # Projekt-Root neben run.py/EXE.
        _root = os.environ.get("IDV_PROJECT_ROOT")
        if not _root:
            _root = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
        path = os.path.join(_root, "config.json")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("SECRET_KEY", "") or "")
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IDV Teams-Scanner – Microsoft Teams/SharePoint via Graph API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python teams_scanner.py --db-path instance/idvault.db --dry-run
  python teams_scanner.py --db-path instance/idvault.db
        """,
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Pfad zur SQLite-DB. Teams-Config + client_secret (Fernet) "
             "werden aus app_settings gelesen.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Listet gefundene Dateien auf, ohne die Datenbank zu ändern",
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="Prüft Konfiguration und Abhängigkeiten, beendet sich danach",
    )
    args = parser.parse_args()

    if not args.db_path:
        print("Fehler: --db-path erforderlich.", file=sys.stderr)
        sys.exit(2)

    # Projektwurzel in sys.path eintragen, damit ``from webapp.secrets``
    # im Subprozess-Kontext funktioniert.
    _proj_root = os.environ.get("IDV_PROJECT_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

    db_path = os.path.abspath(args.db_path)
    if not os.path.isfile(db_path):
        print(f"DB nicht gefunden: {db_path}", file=sys.stderr)
        sys.exit(1)

    secret_key = _load_bootstrap_secret_key()
    config = _load_teams_config_from_db(db_path, secret_key)
    config["log_path"] = os.path.join(
        os.path.dirname(db_path), "logs", "teams_scanner.log"
    )

    try:
        os.makedirs(os.path.dirname(config["log_path"]), exist_ok=True)
    except (OSError, TypeError):
        pass

    logger = setup_logging(config["log_path"])

    # ── --check-config ────────────────────────────────────────────────────
    if args.check_config:
        print("\n=== IDV Teams-Scanner – Konfigurationscheck ===\n")
        _print_check(HAS_MSAL,     "msal installiert            (pip install msal)")
        _print_check(HAS_REQUESTS, "requests installiert        (pip install requests)")
        _print_check(bool(config.get("tenant_id")),     "tenant_id konfiguriert")
        _print_check(bool(config.get("client_id")),     "client_id konfiguriert")
        _print_check(bool(config.get("client_secret")), "client_secret konfiguriert")
        _print_check(bool(config.get("teams")), "Teams/Sites konfiguriert")
        db_dir = os.path.dirname(os.path.abspath(config["db_path"]))
        _print_check(os.path.isdir(db_dir), f"Datenbankpfad erreichbar ({config['db_path']})")
        print()
        sys.exit(0)

    # ── --dry-run ─────────────────────────────────────────────────────────
    if args.dry_run:
        logger.info("DRY-RUN: Keine Datenbankänderungen.")
        token  = get_access_token(config, logger)
        client = GraphClient(token, logger)
        extensions = {e.lower() for e in config.get("extensions", DEFAULT_EXTENSIONS)}

        for team_entry in config.get("teams", []):
            result = resolve_drive(client, team_entry, logger)
            if not result:
                continue
            drive_id, site_url, display_name = result

            url    = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
            params = {
                "$select": (
                    "id,name,size,webUrl,file,deleted,"
                    "createdBy,lastModifiedBy,"
                    "createdDateTime,lastModifiedDateTime,"
                    "parentReference"
                )
            }
            count = 0
            while url:
                page   = client.get(url, params=params)
                params = None
                for item in page.get("value", []):
                    if "file" not in item or "deleted" in item:
                        continue
                    ext = Path(item.get("name", "")).suffix.lower()
                    if ext not in extensions:
                        continue
                    count += 1
                    logger.info(
                        f"  [{display_name}] {item.get('name', '?')} "
                        f"({item.get('size', 0):,} Bytes) – {item.get('webUrl', '')}"
                    )
                url = page.get("@odata.nextLink")

            logger.info(f"[{display_name}] {count} IDV-Dateien gefunden (DRY-RUN)")
        return

    # ── Regulärer Scan ────────────────────────────────────────────────────
    run_teams_scan(config, logger)


if __name__ == "__main__":
    main()
