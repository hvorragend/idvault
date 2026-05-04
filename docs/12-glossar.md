# 12 – Glossar

---

## A

**Active Directory (AD)** – Verzeichnisdienst von Microsoft zur
zentralen Verwaltung von Benutzern, Gruppen und Computern in
Windows-Netzen. In idvscope zur Authentifizierung genutzt.

**AppLocker** – Windows-Mechanismus zur Ausführungskontrolle.
Beschränkt die Ausführung von Anwendungen anhand von Pfaden, Publishern
oder Datei-Hashes.

**Archiviert** – In idvscope: Zustand einer Datei (`idv_files.status`),
die beim letzten Scan nicht mehr gefunden wurde; oder: Zustand einer
IDV (`idv_register.status`), die dauerhaft außer Betrieb genommen wurde.

**Audit-Trail** – Lückenlose, nachträglich unveränderbare Protokollierung
aller relevanten Änderungen (in idvscope: `idv_history`, `login.log`).

**Auto-Escaping** – Automatische Umwandlung potenziell gefährlicher
Zeichen in HTML-Entities durch die Template-Engine (Jinja2) zum
XSS-Schutz.

## B

**BAIT** – Bankaufsichtliche Anforderungen an die IT. Von der BaFin
herausgegebene Rundschreiben, die konkrete IT-Anforderungen an
Kreditinstitute definieren.

**BaFin** – Bundesanstalt für Finanzdienstleistungsaufsicht.

**Bearbeitungsstatus** – Zustand einer Scanner-Datei im
Bearbeitungsprozess (`Neu`, `Zur Registrierung`, `Registriert`,
`Ignoriert`).

**Blueprint** – Flask-Konzept zur modularen Aufteilung einer Webanwendung.
In idvscope: je ein Blueprint pro Funktionsbereich (auth, idv, admin, …).

**Break-Glass** – Notfall-Zugangsmechanismus, der reguläre
Sicherheitsmaßnahmen umgeht, in idvscope als "Notfall-Zugang" im
Admin-Bereich aktivierbar.

## C

**CSRF** – Cross-Site Request Forgery. Angriffstyp, bei dem der
Angreifer den Browser eines angemeldeten Benutzers zu ungewollten
Aktionen verleitet.

**CSP** – Content Security Policy. HTTP-Header zur Abwehr von
XSS-Angriffen.

**CVE** – Common Vulnerabilities and Exposures. Standardisiertes
Identifikationssystem für bekannte Sicherheitslücken.

**CWE** – Common Weakness Enumeration. Standardisiertes
Klassifikationssystem für Softwareschwachstellen.

## D

**DORA** – Digital Operational Resilience Act
(Verordnung (EU) 2022/2554). Seit 17.01.2025 anwendbare EU-Verordnung
zur IKT-Resilienz von Finanzunternehmen.

**DSGVO** – Datenschutz-Grundverordnung
(Verordnung (EU) 2016/679).

**DSFA** – Datenschutz-Folgenabschätzung gemäß Art. 35 DSGVO.

## F

**Fachverantwortlicher** – In idvscope: Rolle der fachlich für eine
IDV verantwortlichen Person.

**Fernet** – Verschlüsselungsverfahren der Python-`cryptography`-
Bibliothek (AES-128-CBC + HMAC-SHA256). In idvscope zur Verschlüsselung
des LDAP-Service-Account-Passworts.

**Funktionstrennung** – Organisatorisches Prinzip, wonach sich
Durchführung, Kontrolle und Freigabe auf verschiedene Personen
verteilen (auch "Segregation of Duties").

## G

**GDA (Grad der Abhängigkeit)** – BAIT-Klassifikationsstufe 1–4,
welche die Abhängigkeit eines Geschäftsprozesses von einer IDV
bemisst.

**Genehmigung** – In idvscope: Zweistufiger Freigabeprozess
(Koordinator, optional IT-Sicherheit/Revision).

## H

**HSTS** – HTTP Strict Transport Security. HTTP-Header, der Browser
anweist, ausschließlich über HTTPS zu kommunizieren.

## I

**IDV** – Individuelle Datenverarbeitung. Von Fachbereichen selbst
entwickelte und betriebene IT-Lösungen (Excel-Arbeitsmappen,
Skripte, Power-BI-Berichte u. a.).

**IDV-Koordinator** – In idvscope: Rolle der zentral für das IDV-Register
verantwortlichen Person.

**IDV-Register** – Zentrale Datenbank aller erfassten IDVs der
Bank; in idvscope Tabelle `idv_register`.

**ISB** – Informationssicherheitsbeauftragter.

**ISO 8601** – Internationaler Standard für Datums- und Zeitangaben.
In idvscope für alle Zeitstempel in der Datenbank verwendet.

**ISO/IEC 27001** – Standard für Informationssicherheits-
Managementsysteme.

## J

**JIT Provisioning** – Just-In-Time-Bereitstellung. Anlegen eines
Benutzerkontos bei der ersten erfolgreichen Authentifizierung statt
im Voraus.

**Jinja2** – Template-Engine der Flask-Anwendung; zuständig für die
HTML-Generierung mit Auto-Escaping.

## K

**Koordinator** – siehe IDV-Koordinator.

## L

**LDAP** – Lightweight Directory Access Protocol. Protokoll zur
Verzeichnisabfrage (hier: Active Directory).

**LDAPS** – LDAP über TLS (Port 636).

## M

**MaRisk** – Mindestanforderungen an das Risikomanagement, BaFin-
Rundschreiben zur Auslegung des § 25a KWG.

**MaGo** – Mindestanforderungen an die Geschäftsorganisation, BaFin-
Rundschreiben für Versicherungsunternehmen.

**Move-Detection** – Erkennung verschobener/umbenannter Dateien durch
den Scanner anhand von Hash und/oder Dateinamen.

## N

**Notfall-Zugang** – Siehe Break-Glass.

## O

**OWASP** – Open Web Application Security Project. Non-profit-
Organisation, bekannt für die OWASP Top 10 (häufigste Sicherheits-
risiken in Webanwendungen).

**Org-Einheit (OE)** – Organisationseinheit; Abteilung/Bereich
innerhalb der Bank.

## P

**PyInstaller** – Build-Werkzeug, das Python-Skripte mit allen
Abhängigkeiten in eine einzelne ausführbare Datei packt.

**Prüfintervall** – Anzahl Monate, nach der eine IDV regelmäßig
geprüft werden soll (Standard: 12).

## R

**Revision** – In idvscope: Rolle der interne Revision, ausschließlich
lesender Zugriff.

**Rollen (idvscope)** – IDV-Administrator, IDV-Koordinator,
Fachverantwortlicher, Revision, IT-Sicherheit.

**Row-Level Security** – Sichtbarkeitsbeschränkung auf Datensatzebene;
in idvscope abhängig von der Rolle und der Zuordnung des Benutzers
zur IDV.

## S

**SAN** – Subject Alternative Name. Feld in X.509-Zertifikaten, das
zusätzliche gültige Hostnamen/IPs enthält.

**Scanner** – In idvscope: Modul zur automatisierten Identifikation
von IDV-Kandidaten im Dateisystem oder in Microsoft Teams.

**SHA-256** – Kryptografische Hashfunktion. In idvscope zur Hash-
Berechnung von Dateien (Scanner) und – als bekannter Schwachpunkt –
zur Speicherung lokaler Passwörter genutzt (Ziel: Migration auf Argon2id).

**Sidecar-Update** – Update-Mechanismus, bei dem neue Codedateien
neben der unveränderten Hauptbinärdatei abgelegt und zur Laufzeit
bevorzugt geladen werden.

**SMTP** – Simple Mail Transfer Protocol. Protokoll zum Versand von
E-Mails.

**SQLite** – Serverlose SQL-Datenbank als eingebettete Bibliothek.
In idvscope als Datenbank-Backend verwendet.

**STARTTLS** – Mechanismus, um eine bestehende Klartextverbindung
(z. B. SMTP auf Port 587) auf TLS zu upgraden.

## T

**Teams-Scanner** – Optionaler idvscope-Scanner für Microsoft Teams /
SharePoint über die Microsoft Graph API.

**Teststatus** – Parallel zum IDV-Status geführtes Feld, das den
Fortschritt im Test- und Freigabeverfahren abbildet.

**TLS** – Transport Layer Security. Protokoll zur verschlüsselten
Datenübertragung (HTTPS, LDAPS, SMTPS).

## U

**UNC-Pfad** – Universal Naming Convention; Netzwerkpfadangabe in der
Form `\\server\freigabe\ordner`.

## V

**VBA** – Visual Basic for Applications. Makro-Sprache in Microsoft-
Office-Anwendungen.

**Versionierung** – In idvscope: Anlegen einer neuen IDV-Version mit
Rückverweis auf die Vorgängerversion; ggf. Auslösen eines neuen
Freigabeverfahrens.

## W

**WAL** – Write-Ahead-Log. SQLite-Modus, der gleichzeitige Lese- und
Schreibzugriffe ermöglicht.

**Wesentlichkeit** – MaRisk-Begriff für die Beurteilung, ob eine IDV
für die Geschäftstätigkeit wesentlich ist; steuert den Umfang des
Freigabeverfahrens.

## X

**XSS** – Cross-Site Scripting. Angriff, bei dem Schadcode (meist
JavaScript) in den Browser eines anderen Benutzers eingeschleust wird.

## Abkürzungen

| Abk. | Bedeutung |
|---|---|
| AD | Active Directory |
| AES | Advanced Encryption Standard |
| BAIT | Bankaufsichtliche Anforderungen an die IT |
| BaFin | Bundesanstalt für Finanzdienstleistungsaufsicht |
| CA | Certificate Authority |
| CIA | Confidentiality, Integrity, Availability |
| CRUD | Create, Read, Update, Delete |
| CSRF | Cross-Site Request Forgery |
| DORA | Digital Operational Resilience Act |
| DSB | Datenschutzbeauftragter |
| DSFA | Datenschutz-Folgenabschätzung |
| DSGVO | Datenschutz-Grundverordnung |
| GDA | Grad der Abhängigkeit |
| GDPR | General Data Protection Regulation (engl. für DSGVO) |
| HGB | Handelsgesetzbuch |
| IDV | Individuelle Datenverarbeitung |
| IKT | Informations- und Kommunikationstechnologie |
| ISB | Informationssicherheitsbeauftragter |
| LDAP | Lightweight Directory Access Protocol |
| LDAPS | LDAP über TLS |
| MaRisk | Mindestanforderungen an das Risikomanagement |
| OWASP | Open Web Application Security Project |
| SMTP | Simple Mail Transfer Protocol |
| TLS | Transport Layer Security |
| UNC | Universal Naming Convention |
| WAL | Write-Ahead Log |
| XSS | Cross-Site Scripting |
