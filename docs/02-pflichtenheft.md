# 02 – Pflichtenheft

**idvault – IDV-Register für Kreditinstitute**

---

## 1 Dokumentidentifikation

| Attribut | Wert |
|---|---|
| Dokumenttyp | Pflichtenheft |
| Version | 1.0.0 |
| Stand | 2026-04-15 |
| Status | Freigegeben |
| Bezug zur Software-Version | idvault 0.1.0 |

## 2 Einleitung

### 2.1 Ausgangslage

Kreditinstitute sind gemäß **MaRisk AT 7.2 Tz. 7** verpflichtet, sämtliche
Individuellen Datenverarbeitungen (IDV) zu identifizieren, zu klassifizieren,
risikoadäquat zu überwachen und regelmäßig zu überprüfen. Für als
**kritisch oder wichtig** eingestufte Funktionen gelten zusätzlich die
Anforderungen der **Verordnung (EU) 2022/2554 (DORA)**.

Bestehende Excel-basierte IDV-Register genügen diesen Anforderungen
nicht mehr, insbesondere hinsichtlich:

- Lückenloser, manipulationssicherer Änderungshistorie
- Rollen- und Rechteverwaltung
- Automatisierter Entdeckung und Klassifizierung
- Prüfungs- und Maßnahmenverfolgung
- Aufsichtlicher Nachweisführung

### 2.2 Zielsetzung

Ziel ist die Bereitstellung einer **eigenständig betreibbaren Anwendung**,
die folgende fachlichen und technischen Grundforderungen erfüllt:

- Betrieb ohne zentrale Server-Infrastruktur (Standalone möglich)
- Keine Abhängigkeit von Cloud-Diensten
- Volle MaRisk-/DORA-/BAIT-Konformität
- Installation in einer Stunde abgeschlossen
- Vollständige deutsche Bedienoberfläche

### 2.3 Abgrenzung

Nicht im Leistungsumfang enthalten:

- Entwicklung oder Ablösung der IDVs selbst
- Ersatz eines Information-Security-Management-Systems (ISMS)
- Risiko-Register der Gesamtbank (nur IDV-Teilbestand)
- Meldewesen-Submission (nur Dokumentation)

## 3 Funktionale Anforderungen (FA)

Anforderungen sind durchnummeriert und referenzierbar. Prioritäten:
- **M** = Muss-Anforderung (zwingend umzusetzen)
- **S** = Soll-Anforderung (umzusetzen, soweit nicht begründet ausgeschlossen)
- **K** = Kann-Anforderung (optional)

### 3.1 IDV-Register (Stammdaten)

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-001 | Das System muss eine eindeutige IDV-Identifikation im Format `IDV-YYYY-NNN` automatisch vergeben. | M |
| FA-002 | Jede IDV muss mit mindestens 30 Attributen (Stammdaten, Klassifizierung, Verantwortliche, Technik) erfasst werden können. | M |
| FA-003 | Das System muss Mehrfach-Dateizuordnungen (1:n) zwischen IDV und Dateien unterstützen. | M |
| FA-004 | Versionen einer IDV müssen unter Erhaltung der Vorgängerhistorie angelegt werden können. | M |
| FA-005 | Das System muss Tags (Freitext-Kategorien) pro IDV unterstützen. | S |
| FA-006 | IDVs müssen ohne Löschung in den Status `Archiviert` überführbar sein. | M |

### 3.2 Klassifizierung und Wesentlichkeit

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-010 | Das System muss die Wesentlichkeit einer IDV anhand konfigurierbarer Kriterien bewerten. | M |
| FA-011 | Das System muss den GDA-Wert (Grad der Abhängigkeit) von 1 bis 4 gemäß BAIT-Orientierungshilfe erfassen. | M |
| FA-012 | Die DORA-Kritikalität muss aus Geschäftsprozess-Kritikalität und GDA ableitbar sein. | M |
| FA-013 | Die Risikoklasse muss einer vorhandenen Klassifikation (Kritisch/Hoch/Mittel/Gering) zugeordnet werden können. | M |
| FA-014 | Verfügbarkeit, Integrität und Vertraulichkeit (CIA-Triade) müssen separat klassifiziert werden. | M |
| FA-015 | Steuerungsrelevanz und Rechnungslegungsrelevanz (§ 239 HGB, § 257 HGB) müssen erfassbar sein. | M |

### 3.3 Workflow

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-020 | Der Status einer IDV muss die Zustände `Entwurf`, `In Prüfung`, `Genehmigt`, `Genehmigt mit Auflagen`, `Abgelehnt`, `Abgekündigt`, `Archiviert` umfassen. | M |
| FA-021 | Jeder Statuswechsel muss mit Zeitstempel, Benutzer-ID und Begründung protokolliert werden. | M |
| FA-022 | Bulk-Statuswechsel für mehrere IDVs gleichzeitig müssen unterstützt werden. | S |
| FA-023 | Das System muss einen Teststatus parallel zum Hauptstatus führen. | M |

### 3.4 Prüfungen und Maßnahmen

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-030 | Das System muss Prüfungen pro IDV mit Prüfungsart, -datum, Prüfer, Ergebnis und Befunden dokumentieren. | M |
| FA-031 | Ein konfigurierbares Prüfintervall (Standard: 12 Monate) muss automatisch das nächste Prüfdatum berechnen. | M |
| FA-032 | Überfällige und bald fällige Prüfungen müssen auf dem Dashboard hervorgehoben werden. | M |
| FA-033 | Maßnahmen (Remediation) müssen einer IDV und optional einer konkreten Prüfung zugeordnet werden. | M |
| FA-034 | Maßnahmen müssen einen Lebenszyklus `Offen → In Bearbeitung → Erledigt` mit Zurückstellung durchlaufen. | M |
| FA-035 | Überfällige Maßnahmen müssen eine E-Mail-Benachrichtigung auslösen. | S |

### 3.5 Test- und Freigabeverfahren

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-040 | Für wesentliche IDVs muss ein vierstufiges Freigabeverfahren (Fachlicher/Technischer Test, Fachliche/Technische Abnahme) durchlaufbar sein. | M |
| FA-041 | Die Phase 2 (Abnahme) darf erst starten, wenn beide Phase-1-Schritte (Tests) bestanden wurden. | M |
| FA-042 | Funktionstrennung: Der als Entwickler eingetragene Mitarbeiter darf keine Freigabeschritte abschließen. | M |
| FA-043 | Nachweise (PDF, XLSX, DOCX etc.) müssen pro Schritt als Datei-Upload hinterlegbar sein. | M |
| FA-044 | Die Freigabe muss als 4-Augen-Genehmigung mit zwei unterscheidbaren Genehmigern erfolgen. | M |
| FA-045 | Bei GDA = 4 oder DORA-kritisch/wichtig ist zusätzlich eine zweite Genehmigungsstufe (IT-Sicherheit/Revision) erforderlich. | M |
| FA-046 | Für wesentliche Eigenentwicklungen muss die Originaldatei revisionssicher archiviert werden (Upload + SHA-256-Prüfsumme, schreibgeschützte Ablage). | M |
| FA-047 | Wenn die Originaldatei nicht verfügbar ist (z.B. Cognos-Bericht in agree21Analysen, serverseitiges Skript ohne Sicherung), muss dies als eigener Statusschritt mit Pflicht-Begründung festgehalten werden. | M |

### 3.6 Scanner-Integration

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-050 | Das System muss einen Dateisystem-Scanner für Netzlaufwerke/UNC-Pfade bereitstellen. | M |
| FA-051 | Der Scanner muss Excel-, Access-, Python-, Power-BI-, SQL- und R-Dateien erkennen. | M |
| FA-052 | Der Scanner muss Makros, externe Verknüpfungen und Blattschutz in Excel-Dateien erkennen. | S |
| FA-053 | Eine Move-/Rename-Detection über SHA-256-Hash muss vorhanden sein. | M |
| FA-054 | Archivierte Dateien müssen erhalten bleiben; Verknüpfungen zum IDV-Register bleiben gültig. | M |
| FA-055 | Das System muss einen Teams/SharePoint-Scanner über Microsoft Graph API bereitstellen. | K |
| FA-056 | Scan-Läufe müssen pausier-, abbrechbar- und wiederaufsetzbar sein (Checkpoint). | S |
| FA-057 | Mehrere Scanner auf unterschiedlichen Rechnern müssen parallel in separate Datenbanken schreiben können. | S |

### 3.7 Authentifizierung

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-060 | Das System muss LDAP/Active-Directory-Authentifizierung über LDAPS (Port 636) unterstützen. | M |
| FA-061 | AD-Gruppen müssen auf idvault-Rollen abbildbar sein (Gruppen-Rollen-Mapping). | M |
| FA-062 | Beim ersten LDAP-Login muss der Benutzer automatisch in der Personen-Tabelle angelegt werden (JIT-Provisioning). | M |
| FA-063 | Bei LDAP-Serverausfall muss automatisch auf lokale Authentifizierung umgeschaltet werden. | M |
| FA-064 | Ein manuell aktivierbarer Notfall-Zugang (Break-Glass) muss vorhanden sein. | M |
| FA-065 | Alle Login-Ereignisse (OK/Fehler) müssen in einem separaten Audit-Log protokolliert werden. | M |

### 3.8 Stammdatenverwaltung

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-070 | Personen, Organisationseinheiten, Geschäftsprozesse und Plattformen müssen anlegbar, editierbar und deaktivierbar sein. | M |
| FA-071 | CSV-Import von Mitarbeiterdaten muss unterstützt werden. | S |
| FA-072 | LDAP-Import aller aktivierten AD-Benutzerkonten muss möglich sein. | S |
| FA-073 | Deaktivierte Stammdaten müssen in Formularen nicht mehr zur Auswahl stehen, in historischen Daten aber erhalten bleiben. | M |

### 3.9 Reporting und Export

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-080 | Ein Excel-Export der vollständigen IDV-Grundgesamtheit muss möglich sein. | M |
| FA-081 | Dashboard-Kennzahlen müssen in Echtzeit aktualisiert werden. | S |
| FA-082 | Listenansichten müssen nach allen relevanten Attributen filterbar und sortierbar sein. | M |
| FA-083 | Das Login-Log muss für Administratoren einsehbar und herunterladbar sein. | M |

### 3.10 Administration

| Nr. | Anforderung | Prio |
|---|---|:---:|
| FA-090 | Die Anwendung muss ohne Serveradministration per Web-Oberfläche konfigurierbar sein. | M |
| FA-091 | Software-Updates müssen ohne Austausch der ausführbaren Datei einspielbar sein (Sidecar-Mechanismus). | S |
| FA-092 | Ein Rollback zum vorherigen Stand muss per Klick möglich sein. | S |
| FA-093 | Die Anwendung muss in abgeschotteten Netzwerken (ohne Internetzugang) betrieben werden können. | M |

## 4 Nicht-funktionale Anforderungen (NFA)

### 4.1 Sicherheit

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-S-01 | Alle Passwörter müssen mit einem kryptografisch sicheren Verfahren gehasht gespeichert werden. | M |
| NFA-S-02 | Service-Account-Passwörter für LDAP müssen symmetrisch verschlüsselt in der Datenbank abgelegt werden. | M |
| NFA-S-03 | Die Transportverschlüsselung (HTTPS/TLS) muss aktivierbar sein; selbstsignierte Zertifikate werden bei Bedarf erzeugt. | M |
| NFA-S-04 | LDAP-Verbindungen müssen ausschließlich über LDAPS (Port 636) erfolgen. | M |
| NFA-S-05 | Session-Cookies müssen bei aktiviertem HTTPS `Secure`, `HttpOnly` und `SameSite=Lax` gesetzt werden. | M |
| NFA-S-06 | Alle Datenbankzugriffe müssen parametrisiert erfolgen (Schutz gegen SQL-Injection). | M |
| NFA-S-07 | Alle HTML-Ausgaben müssen per Auto-Escaping gegen XSS geschützt werden. | M |
| NFA-S-08 | Dateiuploads müssen auf Extension-Whitelist, maximale Größe (32 MB) und Path-Traversal geprüft werden. | M |
| NFA-S-09 | Der Administrationsbereich muss durch Role-based Access Control geschützt sein. | M |
| NFA-S-10 | Fehlgeschlagene Login-Versuche müssen protokolliert und (Soll) gezählt werden. | M |
| NFA-S-11 | CSRF-Schutz für alle verändernden HTTP-Methoden (POST, PUT, DELETE) muss implementiert werden. | M |
| NFA-S-12 | Brute-Force-Schutz (Rate-Limiting) für den Login-Endpunkt muss implementiert werden. | S |

> Anmerkung: NFA-S-01 (sicheres Hashing), NFA-S-11 (CSRF) und NFA-S-12 (Rate-Limiting)
> sind in der aktuellen Version **nicht vollständig** umgesetzt. Siehe
> [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md) mit Remediation-Plan.

### 4.2 Audit und Nachweisführung

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-A-01 | Alle inhaltlichen Änderungen an IDVs müssen in einer append-only History-Tabelle protokolliert werden. | M |
| NFA-A-02 | Jede protokollierte Änderung muss Benutzer-ID, Zeitstempel (UTC, ISO 8601) und geänderte Felder enthalten. | M |
| NFA-A-03 | Protokolle müssen auch nach Benutzerdeaktivierung rekonstruierbar bleiben. | M |
| NFA-A-04 | Login-Protokolle müssen mindestens 12 Monate aufbewahrt werden. | M |
| NFA-A-05 | Protokolle müssen exportierbar sein (Textformat). | M |

### 4.3 Verfügbarkeit und Zuverlässigkeit

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-V-01 | Die Anwendung muss ohne externe Services (Cloud, Internet) betreibbar sein. | M |
| NFA-V-02 | Die Datenbank muss im WAL-Modus betrieben werden, um gleichzeitige Lese-/Schreibzugriffe zu ermöglichen. | M |
| NFA-V-03 | Datenintegrität muss durch Foreign-Key-Constraints und CHECK-Constraints gesichert werden. | M |
| NFA-V-04 | Unerwartete Anwendungsfehler müssen in einer separaten Crash-Log-Datei dokumentiert werden. | M |
| NFA-V-05 | Log-Dateien müssen rotiert werden (Größenlimit), um Plattenfüllung zu verhindern. | M |

### 4.4 Performance

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-P-01 | Die Übersichtslisten müssen bei bis zu 10.000 IDVs in unter 3 Sekunden laden. | M |
| NFA-P-02 | Ein Scan eines Netzlaufwerks mit 100.000 Dateien muss in unter 2 Stunden abschließen. | S |
| NFA-P-03 | Indizes auf hochfrequent gefilterten Spalten (Status, GDA, Prüfdatum) müssen vorhanden sein. | M |
| NFA-P-04 | Das Dashboard muss in unter 1 Sekunde laden. | S |

### 4.5 Portabilität

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-PO-01 | Die Anwendung muss unter Windows 10/11, Windows Server 2019+ und Linux lauffähig sein. | M |
| NFA-PO-02 | Die Anwendung muss als Single-File-Executable (PyInstaller) auslieferbar sein. | M |
| NFA-PO-03 | Die Datenbank muss ohne Datenverlust auf ein anderes System übertragbar sein. | M |
| NFA-PO-04 | Die Anwendung muss ohne Python-Installation auf den Zielrechnern lauffähig sein. | M |

### 4.6 Wartbarkeit

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-W-01 | Änderungen an Python-Modulen müssen ohne Neu-Build der EXE einspielbar sein (Sidecar-Updates). | S |
| NFA-W-02 | Das Datenbankschema muss migrationsfähig sein (idempotente Migrationen). | M |
| NFA-W-03 | Das System muss ohne destruktive Datenbank-Operationen erneut gestartet werden können. | M |
| NFA-W-04 | Die Anwendungssprache (Python 3.10+) muss die Langzeitwartung sicherstellen. | M |

### 4.7 Bedienbarkeit

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-B-01 | Die Bedienoberfläche muss vollständig in deutscher Sprache vorliegen. | M |
| NFA-B-02 | Die Oberfläche muss auf Bildschirmen ab 1366×768 Pixeln bedienbar sein. | M |
| NFA-B-03 | Die Navigation muss an Sidebar und Breadcrumbs orientiert sein. | S |
| NFA-B-04 | Jedes Formularfeld muss eine kontextsensitive Erklärung bereitstellen. | S |

### 4.8 Datenschutz (DSGVO / BDSG)

| Nr. | Anforderung | Prio |
|---|---|:---:|
| NFA-DS-01 | Das System verarbeitet Personenstammdaten (Name, E-Mail, Telefon, AD-Name). Die Verarbeitung ist auf den berechtigten Personenkreis zu beschränken. | M |
| NFA-DS-02 | Personendaten müssen ohne Löschung deaktivierbar sein (Datenhaltung für Audit). | M |
| NFA-DS-03 | Das System darf keine personenbezogenen Daten an externe Dienste übermitteln. | M |
| NFA-DS-04 | Eine Datenschutzfolgeabschätzung (DSFA) ist vor Inbetriebnahme vom Datenschutzbeauftragten zu erstellen. | M |

## 5 Schnittstellen

### 5.1 Externe Schnittstellen

| Schnittstelle | Richtung | Protokoll | Zweck |
|---|---|---|---|
| Active Directory | Ausgehend | LDAPS (Port 636) | Authentifizierung, Benutzerimport |
| SMTP-Server | Ausgehend | SMTP + STARTTLS / SMTPS | E-Mail-Versand |
| Microsoft Graph API | Ausgehend | HTTPS | Teams-/SharePoint-Scan (optional) |
| Netzlaufwerke | Lesend | CIFS/SMB | Dateisystem-Scan |

### 5.2 Interne Schnittstellen

| Komponente | Schnittstelle | Beschreibung |
|---|---|---|
| Scanner → Webapp | Gemeinsame SQLite-Datenbank | Scanner schreibt Funde, Webapp liest sie |
| Webapp → Scanner | Subprocess | Webapp startet Scanner über `--scan`-Flag |
| Update-Upload → Sidecar-Verzeichnis | ZIP-Extraktion | Dateien werden geprüft und im `updates/`-Ordner abgelegt |

## 6 Datenhaltung

| Aspekt | Anforderung |
|---|---|
| Datenbank | SQLite (WAL-Modus); PostgreSQL als Migrationsoption |
| Speicherort | `instance/idvault.db` neben der ausführbaren Datei |
| Backup | Tagesendsicherung per Betriebssystem-Mittel (Kopieren der `.db`-Datei bei geschlossener Anwendung oder per `sqlite3 .backup`) |
| Aufbewahrung | IDV-Register dauerhaft, Logs mindestens 12 Monate |

## 7 Qualitätsanforderungen und Akzeptanzkriterien

| Qualitätsmerkmal | Akzeptanzkriterium |
|---|---|
| Funktionalität | Alle Muss-Anforderungen nachweislich umgesetzt |
| Zuverlässigkeit | Keine Datenkorruption bei abruptem Stromausfall (WAL-Modus) |
| Sicherheit | Externe Pentest-Bewertung mit Restrisiko maximal "mittel" |
| Bedienbarkeit | Erstanwender kann eine IDV innerhalb von 5 Minuten ohne Schulung erfassen |
| Konformität | Mapping gemäß [07 – Aufsichtsrecht](07-aufsichtsrecht.md) vollständig abgedeckt |

## 8 Lieferumfang

| Artefakt | Beschreibung |
|---|---|
| Quellcode | Python 3.10+ (Flask, Jinja2, SQLite) |
| Datenbankschema | `schema.sql` |
| Scanner-Modul | `scanner/idv_scanner.py`, `scanner/teams_scanner.py` |
| Build-Artefakt | `idvault.exe` (PyInstaller, Windows) |
| Dokumentation | Dieser `docs/`-Ordner |
| Installationsanleitung | [06 – Betriebshandbuch](06-betriebshandbuch.md) |

## 9 Inbetriebnahme und Abnahme

### 9.1 Test- und Abnahmeverfahren

| Phase | Inhalt | Verantwortlich |
|---|---|---|
| Komponententest | Unit-Tests (soweit vorhanden) | Entwicklung |
| Systemtest | Funktionale Prüfung aller Muss-Anforderungen | IDV-Koordinator + IT |
| Abnahmetest | Akzeptanztest in produktionsnaher Umgebung | Fachbereich + Revision |
| Sicherheitsprüfung | Schwachstellen- und Penetrationstest | IT-Sicherheit |
| Abnahmebestätigung | Schriftliche Abnahme | Auftraggeber |

### 9.2 Kriterien für den Go-Live

- Alle Muss-Anforderungen (M) umgesetzt und abgenommen
- Schwachstellen mit Severity "kritisch" geschlossen
- Datenschutzfolgeabschätzung vorgenommen
- Berechtigungskonzept durch IT-Sicherheit freigegeben
- Backup- und Rollback-Prozess getestet
- Schulung der IDV-Koordinatoren durchgeführt

## 10 Offene Punkte und Restrisiken

Siehe [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md). Die dort
aufgeführten Maßnahmen sind nach ihrer Priorisierung in die Release-Planung
aufzunehmen. Insbesondere sind vor dem Produktivbetrieb folgende Punkte zu
adressieren:

| Punkt | Maßnahme | Priorität |
|---|---|---|
| SHA-256-Passwort-Hashing ohne Salt | Umstellung auf Argon2id / bcrypt | Kritisch |
| Fehlender CSRF-Schutz | Einführung von Flask-WTF | Kritisch |
| Demo-Zugangsdaten im Code | Deaktivierung vor Produktivstart | Kritisch |
| Default `SECRET_KEY` | Produktiv-Umgebungsvariable setzen | Kritisch |
| Fehlendes Rate-Limiting | Einführung von Flask-Limiter | Hoch |

## 11 Abnahmeerklärung

> Die vorliegende Leistungsbeschreibung (Pflichtenheft) wurde vom
> Auftragnehmer erstellt und durch den Auftraggeber geprüft. Die
> Umsetzung erfolgt nach den hier beschriebenen Anforderungen. Änderungen
> sind nach dem dokumentierten Change-Management-Prozess zu behandeln.

| Rolle | Name | Datum | Unterschrift |
|---|---|---|---|
| Auftraggeber | | | |
| Auftragnehmer | | | |
| IT-Sicherheit | | | |
| Datenschutzbeauftragter | | | |
