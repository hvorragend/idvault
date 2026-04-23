# 05 – Sicherheitskonzept

---

## 1 Zielsetzung und Schutzbedarf

Dieses Sicherheitskonzept dokumentiert die Maßnahmen, mit denen
**Vertraulichkeit, Integrität und Verfügbarkeit (CIA)** der in idvault
verarbeiteten Daten sichergestellt werden. Grundlage sind die
Anforderungen aus **MaRisk AT 7.2**, **BAIT Kapitel 4 und 6**, **DORA**
sowie **ISO/IEC 27001**.

### 1.1 Schutzbedarfsfeststellung

| Schutzziel | Bedarf | Begründung |
|---|---|---|
| Vertraulichkeit | **Hoch** | Enthält Personenstammdaten, Service-Account-Credentials und Informationen zu kritischen IT-Abhängigkeiten der Bank |
| Integrität | **Hoch** | Fehlerhafte oder manipulierte IDV-Einträge gefährden die aufsichtsrechtliche Nachweisführung |
| Verfügbarkeit | **Mittel** | Tagesausfall tolerierbar; keine Echtzeit-Anforderungen |
| Authentizität | **Hoch** | Anmeldung und Protokollierung müssen Personen zweifelsfrei zuordenbar sein |
| Nicht-Abstreitbarkeit | **Mittel** | Append-only History; keine digitalen Signaturen |

### 1.2 Betriebsumfeld

idvault wird **ausschließlich im bankeigenen Intranet** betrieben; die
Anwendung ist **nicht aus dem Internet erreichbar** und nicht über eine
DMZ exponiert. Diese Randbedingung ist Teil des Angreifermodells und
beeinflusst die Bewertung aller in diesem Dokument genannten Maßnahmen.
Details und Konsequenzen für die Schwachstellenbewertung sind in
[09 – Schwachstellenanalyse](09-schwachstellenanalyse.md), Abschnitt 1.3
dokumentiert. Der Intranet-Betrieb ersetzt die hier beschriebenen
Schutzmaßnahmen nicht, sondern ergänzt sie (Defense-in-Depth,
Zero-Trust-Grundsatz auch im internen Netz).

## 2 Identitäts- und Berechtigungsmanagement (IAM)

### 2.1 Authentifizierungsverfahren

idvault kennt drei Authentifizierungspfade:

1. **Primär: LDAP/Active Directory (LDAPS)**
   - Protokoll: LDAPS (Port 636, TLS)
   - Implementierung: `webapp/ldap_auth.py`
   - Passwort des Benutzers verlässt idvault nie im Klartext
   - Service-Account-Passwort: **Fernet-verschlüsselt** (AES-128-CBC + HMAC-SHA256)

2. **Fallback: Lokale Authentifizierung**
   - Aktiviert bei LDAP-Ausfall (automatisch) oder wenn LDAP nicht konfiguriert
   - Speicherung: `persons.password_hash` (aktuell: SHA-256, siehe Restrisiko)

3. **Notfall-Zugang (Break-Glass)**
   - Manuell im Admin-Bereich aktivierbar
   - Umgeht LDAP auch bei erreichbarem Server
   - Nutzung zu dokumentieren; nach Gebrauch zu deaktivieren

### 2.2 Rollen- und Rechtemodell

| Rolle | Schreibrechte | Leserechte | Admin-Funktionen |
|---|---|---|---|
| IDV-Administrator | alle IDVs | alle IDVs | alle |
| IDV-Koordinator | alle IDVs | alle IDVs | Stammdaten (außer Löschen), Scanner-Funde ignorieren/reaktivieren |
| Fachverantwortlicher | IDVs mit eigener Beteiligung (FV, Entwickler, Koordinator oder Stellvertreter) | alle IDVs | — |
| IDV-Entwickler | IDVs mit eigener Beteiligung (wie Fachverantwortlicher); im Freigabeverfahren der betroffenen IDV von Abschluss-/Ablehnungshandlungen ausgeschlossen | alle IDVs | — |
| Revision | — | alle IDVs | — |
| IT-Sicherheit | — | alle IDVs | — |

Durchgesetzt durch Decorator-Funktionen in `webapp/routes/__init__.py`
(`login_required`, `admin_required`, `write_access_required`,
`own_write_required`) sowie die Row-Level-Guards
`user_can_read_idv` / `user_can_write_idv` in `webapp/security.py`.
Die vollständige Funktions-/Rollen-Matrix findet sich in
[01 – Anwendungsdokumentation](01-anwendungsdokumentation.md) Abschnitt 3.2.

### 2.3 Funktionstrennung

Als **IDV-Entwickler** eingetragene Personen (`idv_register.idv_entwickler_id`)
dürfen auf der betroffenen IDV keine Freigabeschritte abschließen oder
ablehnen (4-Augen-Prinzip). Implementiert in
`webapp/routes/freigaben.py::_funktionstrennung_ok` und bei jedem
Abschluss-/Ablehnungsvorgang ausgewertet, sowohl auf der direkten
Abschluss-Route als auch im gemeinsamen Helper
`complete_freigabe_schritt` (der vom Test-Formular aufgerufen wird).

**Ausnahme:** IDV-Administratoren können als organisatorische Eskalation
auch dann Freigabeschritte abschließen, wenn sie gleichzeitig als
Entwickler eingetragen sind. Jeder solche Vorgang wird in `idv_history`
mit dem Aktions-Suffix `_sod_override` und einem Kommentar-Präfix
`[SoD-Ausnahme durch Administrator]` eindeutig markiert, so dass die
Revision die Ausnahmen unmittelbar auswerten kann
(siehe [01 – Anwendungsdokumentation](01-anwendungsdokumentation.md)
§3.4 für die vollständige Liste der Aktions-Suffixe).

### 2.4 JIT-Provisioning

Beim ersten LDAP-Login wird die Person automatisch angelegt; spätere Logins
aktualisieren Stammdaten (Name, E-Mail, Rolle).

### 2.5 Login-freier Self-Service (Owner-Mail-Digest)

Zur Entlastung des IDV-Koordinators kann idvault offene Scanner-Funde
gruppiert an den jeweiligen Dateieigentümer mailen. Der Empfänger erhält
einen Magic-Link auf eine Minimalansicht („Meine Funde"), in der
ausschließlich zwei Aktionen möglich sind: **Ignorieren** oder **Zur
Registrierung vormerken**. Die fachliche IDV-Einordnung bleibt vollständig
beim Koordinator.

Schutzmaßnahmen (Implementierung in `webapp/routes/self_service.py`,
`webapp/tokens.py`, Tabellen `self_service_tokens` und `self_service_audit`):

- **Doppelter Master-Schalter** (Defense-in-Depth): Self-Service ist nur
  aktiv, wenn `config.json["IDV_SELF_SERVICE_ENABLED"] = true` **und** in
  der Admin-UI `self_service_enabled = 1` gesetzt ist. Ist einer der beiden
  Schalter ausgeschaltet, antwortet die Route mit HTTP 404 und es werden
  keine Digest-Mails versendet. Default: beide aus.
- **Magic-Link-Token**: HMAC-SHA256-signiert via
  `itsdangerous.URLSafeTimedSerializer` mit dediziertem Salt
  (`idvault-self-service-v1`, isoliert von Quick-Actions). TTL: 7 Tage.
- **Einmaleintritt & Revoke**: Jeder Token trägt einen serverseitigen jti
  in `self_service_tokens`. Der erste Klick markiert `first_used_at`; ein
  expliziter „Fertig"-Klick oder ein Fehlversand setzt `revoked_at` und
  entwertet den Token. Revokte oder abgelaufene Tokens werden abgelehnt.
- **Zuständigkeits-Filter**: Die Minimalansicht zeigt ausschließlich
  Funde, deren `file_owner` (case-insensitive) zu `persons.user_id`,
  `persons.kuerzel` oder `persons.ad_name` des Token-Inhabers passt. Jede
  POST-Aktion prüft diese Bindung erneut.
- **CSRF**: Alle POST-Formulare tragen den globalen CSRF-Token
  (Flask-WTF); die Session-ID wird nicht mit einem eingeloggten Benutzer
  vermischt (separate Session-Keys `_ss_person_id` / `_ss_jti`).
- **Rate-Limit**: `30 per minute; 200 per hour` pro Client-IP auf den
  Self-Service-Routen, um Brute-Force auf jti-Werte zu verhindern.
- **Dedup**: Pro Empfänger wird innerhalb von
  `self_service_frequency_days` (Default 7 Tage) höchstens ein Digest
  versendet; Eintragung in `notification_log` mit Kind `owner_digest`.
- **Audit**: Jede ausgelöste Aktion wird in `self_service_audit`
  protokolliert (person_id, file_id, aktion, quelle `mail-link`,
  jti, Zeitstempel). Damit bleibt der Vorgang für Revision und
  IDV-Koordinator nachvollziehbar.
- **Keine Querzugriffe**: Die Self-Service-Session gewährt weder
  Leserechte auf andere IDVs/Funde noch auf das reguläre Webinterface;
  sie erlaubt ausschließlich die beiden genannten Aktionen auf Funde des
  Token-Inhabers.

## 3 Transportsicherheit

### 3.1 HTTPS (eingehend)

- Aktivierung: Umgebungsvariable `IDV_HTTPS=1`
- Zertifikatspfade: `IDV_SSL_CERT`, `IDV_SSL_KEY`
- Default: selbstsigniertes Zertifikat (RSA-2048, 10 Jahre, SAN für localhost/Hostname)
- Empfehlung: CA-signiertes Zertifikat aus interner PKI
- Alternative: Reverse-Proxy (nginx/IIS) mit TLS-Terminierung

### 3.2 LDAPS (ausgehend)

- Standardport 636
- Optional Zertifikatsverifikation (`ssl_verify=1`)
- Empfehlung: interne CA als Vertrauensanker einbinden

### 3.3 SMTP (ausgehend)

- STARTTLS (587) oder SMTPS (465) unterstützt
- Kein unverschlüsselter Versand vorgesehen

## 4 Datenverschlüsselung

### 4.1 Data-in-Transit

Siehe Abschnitt 3.

### 4.2 Data-at-Rest

| Datum | Aktuelle Umsetzung |
|---|---|
| LDAP-Service-Passwort | **Fernet-verschlüsselt** (Ableitung: `SHA256(SECRET_KEY)`, Base64) |
| SMTP-Passwort | **Klartext** in `app_settings` (Restrisiko, siehe [09](09-schwachstellenanalyse.md)) |
| Benutzer-Passwörter | SHA-256-Hash (siehe Restrisiko) |
| IDV-Fachdaten | Unverschlüsselt in SQLite |

### 4.3 Schlüsselmanagement

Der `SECRET_KEY` ist zentraler Ausgangspunkt aller symmetrischen
Schlüsselableitungen. Anforderungen:

- Mindestens 32 Zufallsbytes
- Via Umgebungsvariable `SECRET_KEY` zu setzen
- Bei Rotation müssen betroffene Werte (insbesondere LDAP-Bind-Passwort)
  neu eingegeben werden

## 5 Eingabevalidierung und Injektionsschutz

### 5.1 SQL-Injection

- Sämtliche Datenbankzugriffe nutzen **parametrisierte Queries** (`?`-Platzhalter)
- f-String-Konstruktionen in `db.py:186, 190` betreffen ausschließlich
  Spaltennamen aus Code, keine Nutzereingaben
- Code-Review-Empfehlung: statisches Scan-Tool (Bandit) in CI einbinden

### 5.2 Cross-Site Scripting (XSS)

- Jinja2-Auto-Escaping ist systemweit aktiv (`flask` Default)
- Kein Einsatz des `|safe`-Filters oder `Markup()` im Code
- Restrisiko: keine Content-Security-Policy gesetzt; mittelfristig aufzunehmen

### 5.3 Path-Traversal

- Update-ZIP-Upload prüft jeden Eintrag auf `..` und `__pycache__`
- Nur Whitelist-Extensions (`.py`, `.html`, `.json`, `.sql`, `.css`, `.js`)
- Maximale Upload-Größe: 32 MB (`MAX_CONTENT_LENGTH`)

### 5.4 Datei-Uploads (Nachweise)

- Extensions: PNG, JPG, PDF, XLSX, DOCX, CSV, ZIP, TXT (Whitelist in `freigaben.py:33`)
- Dateinamen via `werkzeug.secure_filename()` bereinigt
- Zielordner: `instance/uploads/freigaben/` (außerhalb des Servlet-Roots)

## 6 Audit-Trail und Logging

### 6.1 Login-Log (`instance/login.log`)

- Format: `[OK|FEHLER] Methode IP Benutzer Details`
- Rotation: 2 MB × 10 Segmente
- Abrufbar über Admin-UI (`/admin/login-log`)

### 6.2 Anwendungs-Log (`instance/idvault.log`)

- Level WARNING und höher
- Rotation: 1 MB × 7 Segmente
- Inhalt: Anwendungsereignisse, Fehler, Warnungen

### 6.3 Crash-Log (`instance/idvault_crash.log`)

- Python-Tracebacks bei Startfehlern (insbesondere PyInstaller-EXE)
- Rotation bei > 2 MB

### 6.4 Änderungshistorie (`idv_history`)

- Append-only (UPDATE/DELETE fachlich nicht vorgesehen)
- Felder: `aktion`, `geaenderte_felder` (JSON), `durchgefuehrt_von_id`, `durchgefuehrt_am`, `kommentar`
- Statuswechsel erzeugen zusätzliche Einträge

### 6.5 Datei-History (`idv_file_history`)

- Jeder Scan-Lauf erzeugt Einträge je Datei
- `change_type`: `new`, `unchanged`, `changed`, `moved`, `archiviert`, `restored`
- JSON-`details` bei Move-Ereignissen

### 6.6 Empfehlung Log-Zentralisierung

Die genannten Logs sollten im Produktionsbetrieb per Logshipper
(NXLog, Filebeat, Rsyslog) an das zentrale SIEM übertragen werden, um
Löschung oder Manipulation lokal vorbeugen zu können.

## 7 Sichere Konfiguration für den Produktivbetrieb

Die folgende Checkliste ist vor dem Go-Live zwingend abzuarbeiten.

| Maßnahme | Umsetzung |
|---|---|
| `SECRET_KEY` per Umgebungsvariable setzen (≥ 32 Zufallsbytes) | Pflicht |
| Demo-Zugangsdaten entfernen (`webapp/routes/auth.py:DEMO_USERS = {}`) | Pflicht |
| `DEBUG`-Variable darf nicht gesetzt oder `=0` sein | Pflicht |
| `IDV_HTTPS=1` oder TLS im Reverse-Proxy terminieren | Pflicht |
| Selbstsigniertes Zertifikat durch CA-Zertifikat ersetzen | Empfohlen |
| LDAP mit `ssl_verify=1` betreiben | Pflicht |
| LDAP-Gruppen-Rollen-Mapping vollständig pflegen | Pflicht |
| Default-Passwörter aller Accounts geändert | Pflicht |
| Notfall-Zugang standardmäßig deaktiviert | Pflicht |
| Logs an SIEM weiterleiten | Empfohlen |
| Reverse-Proxy mit HTTP-Security-Headern davor (HSTS, CSP, X-Frame-Options) | Empfohlen |

## 8 Schwachstellenmanagement

Die aktuell bekannten Restrisiken sind in
[09 – Schwachstellenanalyse](09-schwachstellenanalyse.md) ausführlich
dokumentiert. Status der Top-Punkte:

1. **Passwort-Hashing** – ✅ auf `pbkdf2:sha256` mit Salt umgestellt; Legacy-SHA-256-Hashes werden beim Login transparent migriert
2. **Default `SECRET_KEY`** – ✅ Startup-Check erzwingt in Produktion die Umgebungsvariable
3. **Debug-Modus** – ✅ prominente Warnung im Startbanner
4. **SMTP-Passwort** – ✅ Fernet-Verschlüsselung analog LDAP-Bind-Passwort
5. **HTTP-Security-Header** – 🛡 `after_request`-Hook setzt CSP/X-Frame-Options/HSTS; `unsafe-inline` noch in CSP
6. **LDAP-Zertifikatsprüfung** – ✅ Default aktiv; Warnhinweis bei Deaktivierung
7. **Session-Timeouts** – ✅ 4 h Idle-Lifetime + HttpOnly/SameSite/Secure
8. **Kein CSRF-Schutz** – ⏳ offen, Einführung von Flask-WTF geplant (Sprint 1)
9. **Fehlendes Rate-Limiting** – ⏳ offen, Flask-Limiter vorgesehen (Sprint 2)
10. **Demo-Zugangsdaten** – 📋 bewusst beibehalten (dokumentiertes Restrisiko, Betriebsauflage)

## 9 Sicherheits-Tests

| Testart | Frequenz | Verantwortlich |
|---|---|---|
| Statische Code-Analyse (Bandit, Ruff-Security-Regeln) | Bei jedem Commit | Entwicklung |
| Abhängigkeits-Scan (pip-audit, safety) | Monatlich | IT-Sicherheit |
| Penetrationstest (extern) | Jährlich | IT-Sicherheit |
| Berechtigungs-Review | Halbjährlich | IDV-Koordinator + ISB |
| LDAP-Mapping-Review | Quartalsweise | IDV-Administrator |
| Restore-Test der Datenbank-Sicherung | Halbjährlich | Betrieb |

## 10 Incident Response

### 10.1 Vorgehen bei Verdacht auf Kompromittierung

1. Anwendungsinstanz stoppen
2. `instance/login.log` und `instance/idvault.log` sichern
3. Datenbank-Sicherung anfertigen (`.backup`)
4. Ursachenanalyse gemeinsam mit IT-Sicherheit
5. `SECRET_KEY` rotieren; LDAP-Bind-Passwort neu eingeben
6. Betroffene Benutzerpasswörter zurücksetzen
7. Meldung an ISB / Datenschutzbeauftragten gemäß internem IRP

### 10.2 Verantwortlichkeiten

| Rolle | Verantwortung |
|---|---|
| ISB | Koordination, Meldewege, aufsichtliche Pflichtmeldungen (DORA Art. 19) |
| IT-Sicherheit | Technische Analyse, Forensik |
| IDV-Administrator | Anwendung stoppen/starten, Logs bereitstellen |
| Datenschutzbeauftragter | Einschätzung meldepflichtiger Datenschutzvorfälle |

## 11 Datenschutz (DSGVO/BDSG)

- Verarbeitete personenbezogene Daten: Name, E-Mail, Telefon, AD-Name, Rolle, OE-Zuordnung
- Rechtsgrundlage: Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse; regulatorische Pflicht)
- Auftragsverarbeitung: keine externe
- Löschkonzept: Deaktivierung statt Löschung, da Audit-Trail benötigt wird; Hartlöschung nur nach Abstimmung mit DSB
- Export / Auskunftsrecht: über `Administration → Person bearbeiten` einsehbar
- Betroffenenrechte: Meldeweg an DSB, nicht innerhalb der Anwendung

## 12 Referenzen

- MaRisk AT 7.2, Tz. 7 (IDV)
- BAIT Kapitel 4 (Zugriffsrechte), Kapitel 6 (Informationsrisikomanagement)
- DORA Art. 9 (IKT-Sicherheit), Art. 17 (Incident-Management), Art. 28 (Drittdienste)
- ISO/IEC 27001:2022 – Anhang A (u. a. A.5.15 Access Control, A.8.5 Secure Authentication, A.8.24 Cryptography)
- OWASP Top 10 (2021)
- BSI IT-Grundschutz APP.3.1 Webanwendungen
