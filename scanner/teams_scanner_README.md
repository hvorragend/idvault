# IDV Teams-Scanner

Scannt Microsoft Teams-Kanäle und SharePoint Online-Dokumentbibliotheken nach
IDV-Eigenentwicklungen (Excel, Access, Python, SQL, Power BI u.a.), erhebt
Metadaten über die Microsoft Graph API und speichert Ergebnisse in derselben
SQLite-Datenbank wie der Netzlaufwerk-Scanner (`network_scanner.py`).

> **Hintergrund:** Teams-Dateien liegen intern in SharePoint Online. Jeder
> Teams-Kanal hat eine entsprechende SharePoint-Dokumentbibliothek. Der
> Teams-Scanner nutzt diese Tatsache und greift über die Graph API auf alle
> Dateien zu — unabhängig davon, ob die Nutzer über Teams oder direkt über
> SharePoint auf die Dateien zugreifen.

---

## Voraussetzungen

Es gibt zwei Berechtigungs-Modi. Welcher passt, hängt davon ab, wie
der Tenant betrieben wird. Beide Modi sind über den Schalter
*Sites.Selected-Modus* in der Web-UI umschaltbar (siehe
„Administration → Teams-Einstellungen").

### 1a. Standard-Modus (Default)

Azure-AD-App-Registrierung (einmalig durch IT-Administrator):

1. **Azure Portal** → Entra ID → App-Registrierungen → **Neue Registrierung**
   - Name: z.B. `IDVault-Scanner`
   - Kontotyp: *Nur Konten in diesem Organisationsverzeichnis*

2. **API-Berechtigungen** → Microsoft Graph → **Anwendungsberechtigungen** (kein Benutzer-Login):

   | Berechtigung | Zweck |
   |---|---|
   | `Files.Read.All` | Dateien in allen SharePoint-Sites lesen |
   | `Sites.Read.All` | Site-Metadaten abrufen |

3. **Admin-Zustimmung erteilen** (Schaltfläche "Administratorzustimmung erteilen")

4. **Zertifikate & Geheimnisse** → Neuer geheimer Clientschlüssel → Wert kopieren

5. Folgende Werte für die Konfiguration notieren:
   - Verzeichnis-ID (tenant_id)
   - Anwendungs-ID (client_id)
   - Geheimer Clientschlüssel (client_secret)

In diesem Modus sind sowohl Microsoft-Teams-Team-IDs als auch
SharePoint-Site-URLs als Quellen zulässig.

### 1b. Sites.Selected-Modus (für strikt verwaltete Tenants)

Empfohlen für rechenzentrumsbetriebene Tenants, in denen tenantweite
Lese-Permissions wie `Files.Read.All`/`Sites.Read.All` gesondert
bewertet werden. Der Tenant-Admin entscheidet pro Site, ob die App
zugreifen darf — standardmäßig hat sie keinen Zugriff.

**Schritt A — einmalig pro Tenant:**

1. App-Registrierung wie oben anlegen.
2. Application-Permission: ausschließlich `Sites.Selected`.
   `Files.Read.All`, `Sites.Read.All` und `Group.Read.All` werden in
   diesem Modus nicht benötigt und sollten nicht vergeben werden.
3. Admin-Zustimmung erteilen.
4. Client-ID, Tenant-ID und Client-Secret in idvault hinterlegen.
5. Schalter „Sites.Selected-Modus" in der Web-UI aktivieren.

**Schritt B — einmalig pro SharePoint-Site, die gescannt werden soll:**

Der Tenant-Admin gibt der App pro Site explizit Lese-Rechte. Beispiel
mit `curl` (Token mit `Sites.FullControl.All` oder
SharePoint-Admin-Account):

```bash
SITE_ID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://graph.microsoft.com/v1.0/sites/{tenant}.sharepoint.com:/sites/{site-name}" \
  | jq -r .id)

curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  "https://graph.microsoft.com/v1.0/sites/$SITE_ID/permissions" \
  -d '{
        "roles": ["read"],
        "grantedToIdentities": [
          { "application": { "id": "<client-id>", "displayName": "idvault" } }
        ]
      }'
```

Alternativ per PnP-PowerShell:

```powershell
Grant-PnPAzureADAppSitePermission `
  -AppId      "<client-id>" `
  -DisplayName "idvault" `
  -Site       "https://{tenant}.sharepoint.com/sites/{site-name}" `
  -Permissions Read
```

Anschließend in idvault die Site-URL als Quelle eintragen. Team-IDs
werden in diesem Modus übersprungen — ist eine Teams-Site gemeint,
bitte deren SharePoint-Site-URL eintragen.

**Diagnose:** Fehlt der Site-Grant, antwortet Graph mit HTTP 403; der
Scanner protokolliert in dem Fall einen klar formulierten Hinweis auf
den fehlenden `POST /sites/{site-id}/permissions`-Aufruf.

### 2. Python-Abhängigkeiten

```cmd
pip install msal requests
```

Oder über die Projektdatei:

```cmd
pip install -r requirements.txt
```

---

## Konfigurationsort

Alle Einstellungen des Teams-Scanners werden in der SQLite-Datenbank
(`app_settings`-Tabelle) gehalten und über die Web-UI
`Administration → Teams-Einstellungen` gepflegt. Das Clientgeheimnis wird
dort mit Fernet (AES-128) verschlüsselt abgelegt (`teams_client_secret_enc`);
der Schlüssel stammt aus `SECRET_KEY` in `config.json`.

Die Webapp startet den Scanner als Subprocess mit `--db-path <pfad>`; der
Scanner liest sämtliche Einstellungen und das Geheimnis selbstständig aus der
DB.

## Schnellstart (aus der Web-UI)

1. In der Admin-Oberfläche unter **Scanner → Teams-Einstellungen** Tenant-ID,
   Client-ID und Clientgeheimnis eintragen, Teams/Sites hinzufügen und
   speichern.
2. Scan per Schaltfläche starten – der Subprocess protokolliert nach
   `instance/logs/teams_scanner.log`.

## Standalone-Aufruf (Debug / Scheduled Task)

```cmd
python teams_scanner.py --db-path instance\idvault.db
python teams_scanner.py --db-path instance\idvault.db --dry-run
python teams_scanner.py --db-path instance\idvault.db --check-config
```

Voraussetzung: `teams_config` und `teams_client_secret_enc` wurden zuvor
über die Web-UI befüllt. Der Log-Pfad leitet sich aus dem DB-Pfad ab
(`<db_parent>/logs/teams_scanner.log`).

---

## Konfiguration (`app_settings['teams_config']`)

Intern als JSON-Blob in der Tabelle `app_settings`:

```json
{
  "tenant_id":          "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "client_id":          "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "hash_size_limit_mb": 100,
  "download_for_ooxml": true,
  "move_detection":     "name_and_hash",
  "extensions": [
    ".xlsx", ".xlsm", ".xlsb", ".xltm", ".xltx",
    ".accdb", ".mdb",
    ".pbix", ".pbit",
    ".py", ".r", ".rmd",
    ".sql"
  ],
  "teams": [
    { "team_id":  "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "display_name": "IDV-Team Markt" },
    { "site_url": "https://contoso.sharepoint.com/sites/Controlling", "display_name": "Controlling" }
  ]
}
```

Das Clientgeheimnis liegt getrennt davon verschlüsselt in
`app_settings['teams_client_secret_enc']`.

### Parameter-Referenz

| Parameter | Typ | Standard | Beschreibung |
|---|---|---|---|
| `tenant_id` | String | – | Azure-Verzeichnis-ID (aus App-Registrierung) |
| `client_id` | String | – | Anwendungs-ID (aus App-Registrierung) |
| `client_secret` | String | – | Clientgeheimnis (Fernet-verschlüsselt in `teams_client_secret_enc`) |
| `hash_size_limit_mb` | Integer | `100` | Dateien größer als dieser Wert werden nicht heruntergeladen |
| `download_for_ooxml` | Boolean | `true` | Dateien für OOXML-Analyse herunterladen (Makros, Formeln etc.) |
| `move_detection` | String | `"name_and_hash"` | Modus der Verschiebe-Erkennung (identisch zu `network_scanner.py`) |
| `extensions` | Liste | (s.o.) | Erfasste Dateierweiterungen |
| `teams` | Liste | `[]` | Zu scannende Teams oder SharePoint-Sites |
| `sites_selected_mode` | Boolean | `false` | Strict-Modus: App-Registrierung nutzt nur `Sites.Selected` (Lese-Rechte pro Site explizit vom Tenant-Admin gegrantet). Team-IDs werden in diesem Modus übersprungen. |

#### `teams`-Einträge

Jeder Eintrag hat entweder `team_id` **oder** `site_url`:

```json
{ "team_id":  "...", "display_name": "Teamname (optional)" }
{ "site_url": "https://contoso.sharepoint.com/sites/...", "display_name": "..." }
```

---

## Integration mit der idvault-Webapp

Scanner und Webapp teilen sich dieselbe SQLite-Datenbank. Der Subprocess
erhält den Pfad via `--db-path`. Im idvault-Interface erscheinen
Teams/SharePoint-Dateien unter
**Scanner → Entdeckte Dateien** mit der Quellenangabe `sharepoint` in der
neuen Spalte `source`.

---

## Sync-Verhalten und Delta-Queries

Der Teams-Scanner nutzt die **Graph API Delta-Query**, um Änderungen
effizient zu erkennen, ohne bei jedem Lauf alle Dateien herunterladen zu müssen.

### Erster Lauf (Vollscan)

```
GET /drives/{id}/root/delta
→ Alle Dateien werden zurückgegeben.
→ Delta-Token wird am Ende in teams_delta_tokens gespeichert.
→ Dateien, die nicht (mehr) vorhanden sind, werden via mark_deleted_files()
  archiviert.
```

### Folgeläufe (Inkrementalscan)

```
GET {deltaLink aus letztem Lauf}
→ Nur Änderungen seit dem letzten Lauf werden zurückgegeben:
    - Neue oder geänderte Dateien → werden normal verarbeitet (upsert)
    - Gelöschte Dateien → werden sofort als 'archiviert' markiert
→ Neuer Delta-Token wird gespeichert.
```

**Vorteil:** Auch sehr große Bibliotheken mit tausenden von Dateien können
inkrementell und performant überwacht werden.

**Hinweis:** Beim ersten Lauf nach einer längeren Pause oder nach manuellem
Löschen des Delta-Tokens wird automatisch ein neuer Vollscan durchgeführt.

---

## Datei-Status und Änderungshistorie

Das Statusmodell ist identisch zum Netzlaufwerk-Scanner:

| Status | Bedeutung |
|---|---|
| `active` | Datei wurde im letzten Scan gefunden |
| `archiviert` | Datei wurde gelöscht oder nicht mehr gefunden |

### Status-Übergänge

```
Erstfund (Graph: neue Datei)
  → active  (change_type: new)

Datei geändert (Graph: lastModifiedDateTime oder Hash geändert)
  active → active  (change_type: changed)

Datei nicht geändert
  active → active  (change_type: unchanged)

Datei gelöscht (Graph: "deleted"-Markierung im Delta)
  active → archiviert  (change_type: archiviert)

Datei wiederhergestellt (Graph: Datei wieder vorhanden)
  archiviert → active  (change_type: restored)

Datei verschoben / umbenannt (Move-Detection aktiv)
  active (alte URL) → active (neue URL)  (change_type: moved)
  ↳ DB-ID und IDV-Register-Verknüpfung bleiben erhalten.
```

---

## OOXML-Analyse (Makros, Formeln, Blattschutz)

Für Office-Dateien (`.xlsx`, `.xlsm`, `.xlsb`, `.docx` etc.) wird dieselbe
OOXML-Analyse wie beim Netzlaufwerk-Scanner durchgeführt. Dazu wird die Datei
**temporär heruntergeladen**, analysiert und dann sofort wieder gelöscht.

Folgende Merkmale werden erkannt:

| Merkmal | Spalte in `idv_files` |
|---|---|
| VBA-Makros vorhanden | `has_macros` |
| Externe Verknüpfungen | `has_external_links` |
| Anzahl Tabellenblätter | `sheet_count` |
| Anzahl benannter Bereiche | `named_ranges_count` |
| Anzahl Formelzellen | `formula_count` |
| Blattschutz aktiv | `has_sheet_protection` |
| Blattschutz mit Passwort | `sheet_protection_has_pw` |
| Arbeitsmappenschutz | `workbook_protected` |

**Download überspringen:**
Mit `"download_for_ooxml": false` werden keine Dateien heruntergeladen — die
OOXML-Felder bleiben leer (0 / null). Sinnvoll, wenn Bandbreite limitiert ist
oder nur Dateilisten ohne Inhaltsprüfung benötigt werden.

**Größenlimit:**
Dateien, die `hash_size_limit_mb` überschreiten, werden nicht heruntergeladen.
Der SHA-256-Hash wird dann aus den Graph-API-Metadaten übernommen (sofern von
Microsoft bereitgestellt), andernfalls als `HASH_ERROR` gesetzt.

---

## Vergleich: Netzlaufwerk-Scanner vs. Teams-Scanner

| Merkmal | `network_scanner.py` | `teams_scanner.py` |
|---|---|---|
| Dateiablage | Netzlaufwerke / UNC-Pfade | Teams-Kanäle / SharePoint Online |
| Authentifizierung | Windows-Dienstkonto | Azure AD App-Registrierung |
| Plattform | Windows (bevorzugt) | Plattformunabhängig |
| Hash-Berechnung | Lokal (SHA-256) | Lokal nach Download oder aus Graph |
| OOXML-Analyse | Lokal (direkt) | Nach temporärem Download |
| Änderungserkennung | Vollscan mit mtime-Filter | Graph API Delta-Query |
| Echtzeit-Benachrichtigungen | Nein | Delta-Token (nahezu echtzeiterfahig) |
| Dateieigentümer | Windows SID / AD-Name | AAD-Benutzername |
| `full_path` in DB | UNC-Pfad oder Laufwerkspfad | SharePoint-URL (webUrl) |
| `share_root` in DB | UNC-Root / Laufwerksbuchstabe | SharePoint-Site-URL |
| `source` in DB | `filesystem` | `sharepoint` |

Beide Scanner schreiben in **dieselben Tabellen** (`idv_files`, `idv_file_history`,
`scan_runs`) — die Ergebnisse sind im idvault-Interface gemeinsam auswertbar.

---

## Datenbankschema-Erweiterungen

Der Teams-Scanner ergänzt beim ersten Start automatisch (idempotent):

```sql
-- Herkunft der Datei
ALTER TABLE idv_files ADD COLUMN source TEXT DEFAULT 'filesystem';

-- Stabiler SharePoint-Item-ID (bleibt bei Umbenennung/Verschieben erhalten)
ALTER TABLE idv_files ADD COLUMN sharepoint_item_id TEXT;

-- Delta-Token-Speicher pro Drive
CREATE TABLE IF NOT EXISTS teams_delta_tokens (
    drive_id    TEXT PRIMARY KEY,
    delta_token TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

Bestehende Netzlaufwerk-Einträge erhalten automatisch `source = 'filesystem'`.

---

## Fehlerbehandlung und Throttling

Die Graph API begrenzt die Anzahl der Anfragen pro Zeiteinheit. Der Scanner
behandelt HTTP-429- und HTTP-503-Antworten automatisch mit **exponentiellem
Backoff**:

```
Versuch 1 → 429 → warte Retry-After-Header (mind. 2s)
Versuch 2 → 429 → warte 4s
Versuch 3 → 429 → warte 8s
Versuch 4 → 429 → warte 16s
Versuch 5 → Fehler wird geloggt
```

Dateien mit Verarbeitungsfehlern werden in `scan_runs.errors` gezählt und
im Log protokolliert. Der Scan läuft für die übrigen Dateien weiter.

---

## Als Scheduled Task (Windows)

```
1. Aufgabenplanung öffnen
2. Neue Aufgabe:
   python C:\IDV-Scanner\teams_scanner.py --db-path C:\IDV-Scanner\instance\idvault.db
3. Trigger: wöchentlich (z. B. Dienstag 07:00 Uhr, versetzt zum Netzlaufwerk-Scan)
4. Ausführen als: Dienstkonto (benötigt nur HTTPS-Internetzugriff auf
   graph.microsoft.com und login.microsoftonline.com). Das Clientgeheimnis
   wird aus der SQLite-DB gelesen – keine zusätzlichen Umgebungsvariablen
   nötig.
```

---

## CLI-Referenz

```
python teams_scanner.py --help

  --db-path PATH       Pfad zur SQLite-Datenbank (Pflicht im Normalbetrieb;
                       Konfiguration wird aus app_settings gelesen)
  --dry-run            Listet gefundene Dateien auf, ohne DB zu ändern
  --check-config       Prüft Abhängigkeiten und Konfiguration
```

---

## Häufige Fehler

| Fehlermeldung | Ursache | Lösung |
|---|---|---|
| `MSAL nicht installiert` | msal fehlt | `pip install msal` |
| `Token-Anfrage fehlgeschlagen: AADSTS700016` | client_id falsch | App-Registrierung prüfen |
| `Token-Anfrage fehlgeschlagen: AADSTS7000215` | client_secret falsch/abgelaufen | Neues Geheimnis anlegen |
| `403 Forbidden` bei Graph-Anfragen | Admin-Zustimmung fehlt | IT-Admin: "Administratorzustimmung erteilen" |
| `Kein Clientgeheimnis gespeichert` | Secret in der Web-UI nicht eingetragen | Unter /admin/teams-einstellungen erneut speichern |
| `Keine Dokumentbibliothek gefunden` | Site-URL falsch oder keine Berechtigung | site_url und Sites.Read.All prüfen |

---

## Datenbankschema (Überblick)

```
scan_runs            – ein Eintrag pro Scan-Lauf mit Statistik
idv_files            – eine Zeile pro bekannter Datei (aktiv oder archiviert)
                       Neu: source, sharepoint_item_id
idv_file_history     – lückenlose Änderungshistorie pro Datei und Scan-Lauf
teams_delta_tokens   – Delta-Token pro Drive für inkrementellen Sync
```

Die `id`-Spalte in `idv_files` wird **niemals geändert** — auch nicht bei
Verschiebung oder Umbenennung. Die Verknüpfung
`idv_register.file_id → idv_files.id` bleibt dauerhaft konsistent.
