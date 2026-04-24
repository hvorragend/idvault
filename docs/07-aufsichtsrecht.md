# 07 – Aufsichtsrechtliche Konformität

---

## 1 Geltungsbereich

Dieses Dokument ordnet die Funktionen der Anwendung idvault den relevanten
aufsichtsrechtlichen Normen zu. Es dient als Nachweisgrundlage für die
interne Revision, Wirtschaftsprüfer, die Bundesanstalt für
Finanzdienstleistungsaufsicht (BaFin) und die Deutsche Bundesbank.

Abgedeckte Regelwerke:

| Kürzel | Titel | Stand |
|---|---|---|
| **MaRisk** | Mindestanforderungen an das Risikomanagement | aktuelle BaFin-Fassung |
| **BAIT** | Bankaufsichtliche Anforderungen an die IT | aktuelle BaFin-Fassung |
| **DORA** | Digital Operational Resilience Act (Verordnung (EU) 2022/2554) | 17.01.2025 anwendbar |
| **MaGo** | Mindestanforderungen an die Geschäftsorganisation | aktuelle BaFin-Fassung |
| **KAIT** | Kapitalverwaltungsaufsichtliche Anforderungen an die IT | aktuelle BaFin-Fassung |
| **DSGVO** | Datenschutz-Grundverordnung | seit 25.05.2018 |
| **HGB** | Handelsgesetzbuch, § 239, § 257 | aktuelle Fassung |
| **ISO/IEC 27001** | Information Security Management Systems | 2022 |

## 2 Mapping MaRisk AT 7.2 Tz. 7 (IDV)

Die Ziffer AT 7.2 Tz. 7 schreibt vor, dass IDVs ein geordnetes Register
führen, das eine Risikobewertung, angemessene Kontrollen und eine
Dokumentation des Entwicklungs-, Test-, Freigabe- und Änderungsprozesses
umfasst.

| Anforderung MaRisk | Umsetzung in idvault | Nachweis |
|---|---|---|
| Identifikation aller IDVs | Dateisystem-Scanner entdeckt Kandidaten; Erfassung im Register | [10 – Scanner](10-scanner.md), [04 – Datenmodell](04-datenmodell.md) `idv_register` |
| Vollständigkeit der Grundgesamtheit | Scanner-Eingang (`bearbeitungsstatus=Neu`) + Dashboard-Ansicht "Unvollständig" | `v_unvollstaendige_idvs` View |
| Risikoklassifikation | GDA-Wert (1-4) nach BAIT-Orientierungshilfe; CIA-Bewertung; Risikoklassen | `idv_register.gda_wert`, `risikoklasse_id` |
| Wesentlichkeitsbeurteilung | Konfigurierbarer Kriterienkatalog | `wesentlichkeitskriterien`, `idv_wesentlichkeit` |
| Steuerungs- und Rechnungslegungsrelevanz | Separate Felder mit Begründung | `steuerungsrelevant`, `rechnungslegungsrelevant` |
| Berechtigungsverwaltung | Rollenkonzept (5 Rollen), LDAP-Integration | [05 – Sicherheitskonzept](05-sicherheitskonzept.md) |
| Entwicklungs-, Test-, Freigabeverfahren | 5-stufiges Freigabeverfahren in 3 Phasen (Test / Abnahme / Archivierung); für *nicht-wesentliche* IDVs optional „Stille Freigabe" als verkürztes Verfahren (Issue #351) | `idv_freigaben`, `fachliche_testfaelle`, `technischer_test`, `idv_register.freigabe_verfahren` |
| Revisionssichere Archivierung der Originaldatei | Phase 3 – Upload + SHA-256 oder dokumentierte Nicht-Verfügbarkeit (z.B. Cognos) | `idv_freigaben.datei_verfuegbar`, `archiv_datei_pfad`, `archiv_datei_sha256` |
| Funktionstrennung | Entwickler darf eigene IDV nicht freigeben | `webapp/routes/freigaben.py` |
| Dokumentation | Verpflichtendes `dokumentation_vorhanden`-Flag | `idv_register` |
| Regelmäßige Überprüfungen | Prüfintervall konfigurierbar (Standard 12 Monate); Fälligkeits-Dashboard | `pruefungen`, `v_prueffaelligkeiten` |
| Maßnahmenverfolgung | Status-Workflow Offen→In Bearbeitung→Erledigt | `massnahmen` |
| Änderungsverfolgung | Append-only History | `idv_history` |
| 4-Augen-Prinzip | Zweistufige Genehmigung (Koordinator + IT-Sicherheit bei GDA 4) | `genehmigungen` |
| Versionierung | "Neue Version" erzeugt IDV-Kopie mit Rückverweis | `idv_register.vorgaenger_idv_id` |

### 2.1 Stille Freigabe für nicht-wesentliche IDVs (Issue #351)

Für Eigenentwicklungen mit Klassifikation **nicht wesentlich** ist das
fünfstufige Test-/Freigabeverfahren regulatorisch nicht zwingend
vorgeschrieben (MaRisk AT 7.2 Tz. 7 fordert das volle Verfahren nur für
wesentliche IDVs). idvault stellt deshalb optional die **Stille Freigabe**
zur Verfügung — ein verkürztes Verfahren in drei Schritten:

1. **Selbstzertifizierung des Entwicklers** (1 Klick): bestätigt
   Funktion und Korrektheit. Audit-Eintrag
   `silent_release_self_certified` in `idv_history`.
2. **Sicht-Freigabe Fachverantwortlicher** (per Magic-Link, 2 Klicks):
   HMAC-signierter Link mit 7 Tagen TTL; Audit-Eintrag
   `silent_release_supervisor_acknowledged`.
3. **Automatische Archivierung mit SHA-256**: sofern eine Hauptdatei
   verknüpft ist; Audit-Eintrag `silent_release_archived`.

Status nach Abschluss: `Freigegeben (Stille Freigabe)`. In Reports und
im Excel-Export ist das Verfahren über die Spalte „Freigabe-Verfahren"
(Werte: `Standard` / `Stille Freigabe`) erkennbar.

**MaRisk-Konformität**: Das Vier-Augen-Prinzip wird eingehalten
(Entwickler → Fachverantwortlicher), die Funktionstrennung ebenfalls
(Selbstzertifizierung ≠ Sicht-Freigabe). Vollständiger Audit-Trail
bleibt erhalten. Default ist die Stille Freigabe **deaktiviert**
(`app_settings.silent_release_enabled='0'`); jedes Institut entscheidet
in eigener Verantwortung über die Aktivierung.

## 3 Mapping BAIT

Die BAIT adressieren die IT-Steuerung, Informationsrisikomanagement,
Berechtigungen und IT-Projekte. Relevant für idvault sind insbesondere
Kapitel 4 (Zugriffsrechte) und Kapitel 6 (Informationsrisikomanagement).

| BAIT-Kapitel | Anforderung | Umsetzung |
|---|---|---|
| Kapitel 2 (IT-Strategie) | IDV-Management als Bestandteil der IT-Governance | idvault als zentrales Register |
| Kapitel 4 (Benutzerberechtigungsmanagement) | Need-to-know, Funktionstrennung, regelmäßiges Rezertifizieren | Rollenkonzept, LDAP, Deaktivierung |
| Kapitel 5 (IT-Projekte) | Test- und Freigabeverfahren | `idv_freigaben` |
| Kapitel 6 (Informationsrisikomanagement) | Schutzbedarf (CIA), Risikoklassifikation | `verfuegbarkeit`, `integritaet`, `vertraulichkeit`, `risikoklasse_id` |
| Kapitel 7 (Informationssicherheitsmanagement) | Logging, Monitoring | Login-Log, App-Log, History |
| Kapitel 10 (IDV) | Kerngebiet; vollständige Umsetzung | Gesamtanwendung |

### 3.1 BAIT-Orientierungshilfe zur IDV-Risikoklassifizierung

Die BAIT-Orientierungshilfe definiert den **Grad der Abhängigkeit (GDA)**
in vier Stufen:

| GDA | Bezeichnung | Bedeutung | Umsetzung |
|---|---|---|---|
| 1 | Unterstützend | Prozess läuft auch ohne IDV, mit erhöhtem manuellem Aufwand | Pflicht zur Dokumentation |
| 2 | Relevant | IDV unterstützt den Prozess; alternative Durchführung möglich | Zusätzlich: Regelprüfung |
| 3 | Wesentlich | Kernprozessunterstützung; keine vollständige manuelle Alternative | Zusätzlich: Test- und Freigabeverfahren |
| 4 | Vollständig abhängig | Prozess kann ohne diese IDV nicht ausgeführt werden | Zusätzlich: zweite Genehmigungsstufe IT-Sicherheit/Revision, verpflichtende DORA-Bewertung |

## 4 Mapping DORA (Verordnung (EU) 2022/2554)

| DORA-Artikel | Anforderung | Umsetzung |
|---|---|---|
| Art. 5 (IKT-Risikomanagementrahmen) | Governance, Rollen | Rollenkonzept, Verantwortlichkeiten je IDV |
| Art. 6 (IKT-Risikomanagement) | Identifizierung, Schutz, Erkennung | Scanner, Klassifizierung, Prüfungen |
| Art. 8 (Identifizierung kritischer Funktionen) | Erfassung IKT-gestützter Geschäftsprozesse und deren Abhängigkeiten | `geschaeftsprozesse.ist_kritisch`, `idv_register.dora_kritisch_wichtig` |
| Art. 9 (Schutz und Prävention) | Zugriffskontrolle, Verschlüsselung | LDAP, HTTPS, Fernet |
| Art. 10 (Erkennung) | Monitoring | Logs, Dashboard |
| Art. 17 (IKT-Vorfallsmanagement) | Klassifizierung und Meldung | Incident-Response-Prozess in [05 – Sicherheitskonzept](05-sicherheitskonzept.md) |
| Art. 28 (Drittanbieter) | — | Nicht direkt in idvault; IDVs können aber Drittanbieter-Leistungen referenzieren |
| Art. 30 (Vertragsauflagen) | Nachweise über Betrieb/Abhängigkeiten | Dokumentationsfelder in `idv_register` |

### 4.1 Kritisch- oder Wichtig-Bewertung

Das Feld `dora_kritisch_wichtig` wird aus folgenden Kriterien abgeleitet:

```
dora_kritisch_wichtig = 1 WENN
    geschaeftsprozess.ist_kritisch = 1
    UND gda_wert >= 3
```

Die Logik ist in `db.py` (Funktion zur DORA-Ableitung) umgesetzt und kann
durch Administratoren per Override übersteuert werden, sofern begründet.

### 4.2 Änderungskategorie und verschlankter Patch-Workflow

Bei einer neuen Version einer bereits freigegebenen IDV wird die
**Änderungskategorie** erfasst (`idv_register.freigabe_aenderungskategorie`):

| Kategorie | Verfahrensumfang |
|---|---|
| `grundlegend` | Vollständiges Verfahren: Tests (Fachlicher Test + Technischer Test) und Abnahmen (Fachliche Abnahme + Technische Abnahme) plus obligatorische Archivierung der Originaldatei |
| `patch` | Verkürzter Workflow gemäß Admin-Konfiguration (`app_settings.freigabe_patch_schritte`), Default: `Technischer Test`, `Fachliche Abnahme`, `Archivierung Originaldatei` |

Regeln:

- Erstfreigaben sind zwingend `grundlegend` (kein Vorgänger vorhanden).
- `patch` ist für IDVs mit GDA&nbsp;=&nbsp;4 oder DORA-kritisch/wichtig
  **gesperrt**; der volle Workflow bleibt Pflicht (FA-045 in
  [02 – Pflichtenheft](02-pflichtenheft.md)).
- Die Einstufung als `patch` erfordert eine begründete Freitext-Angabe
  (`idv_register.freigabe_patch_begruendung`), die zusammen mit der
  Aktion `freigabe_gestartet_patch` bzw. `freigabe_gestartet` im
  `idv_history`-Audit-Trail abgelegt wird.
- Läuft ein Verfahren bereits, ändert eine spätere Konfig-Änderung am
  Patch-Schrittekatalog den Umfang des laufenden Verfahrens nicht (die
  Kategorie wird beim Start festgeschrieben).

## 5 Mapping ISO/IEC 27001:2022

| ISO-Kontrolle | Umsetzung |
|---|---|
| A.5.15 Access Control | Rollenmodell, Decorator-basiert |
| A.5.17 Authentication Information | Passwort-Hashing, LDAP-Integration |
| A.8.2 Privileged Access Rights | Admin-Rolle getrennt, Audit |
| A.8.5 Secure Authentication | LDAPS, Session-Management |
| A.8.6 Capacity Management | Log-Rotation, SQLite-WAL |
| A.8.12 Data Leakage Prevention | Fernet-Verschlüsselung, TLS |
| A.8.15 Logging | login.log, idvault.log, idv_history |
| A.8.16 Monitoring Activities | Dashboard, Log-Viewer, SIEM-Export |
| A.8.23 Web Filtering | — (durch Infrastruktur bereitgestellt) |
| A.8.24 Use of Cryptography | Fernet, TLS, SHA-256 (Hash-Migration empfohlen) |
| A.8.28 Secure Coding | Dokumentiert in [08 – Quellcodeanalyse](08-quellcodeanalyse.md) |
| A.8.29 Security Testing | Dokumentiert in [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md) |

## 6 Mapping DSGVO / BDSG

| DSGVO-Artikel | Thema | Umsetzung |
|---|---|---|
| Art. 5 | Grundsätze (Rechtmäßigkeit, Richtigkeit, Speicherbegrenzung) | Erläutert in [05 – Sicherheitskonzept](05-sicherheitskonzept.md) §11 |
| Art. 6 | Rechtsgrundlage (lit. f – berechtigtes Interesse) | Regulatorische Pflicht der Bank |
| Art. 15 | Auskunftsrecht | Einsehbar über Personenverwaltung |
| Art. 17 | Löschung | Soft-Delete; Hartlöschung in Abstimmung mit DSB |
| Art. 25 | Privacy by Design | Minimal-Datenmodell, Rollenkonzept |
| Art. 30 | Verzeichnis der Verarbeitungstätigkeiten | Bereitgestellt durch DSB (außerhalb idvault) |
| Art. 32 | Technische/organisatorische Maßnahmen | Gesamtes Sicherheitskonzept |
| Art. 33/34 | Meldewege bei Datenschutzverletzungen | Incident-Response in [05](05-sicherheitskonzept.md) §10 |
| Art. 35 | Datenschutz-Folgenabschätzung (DSFA) | Pflicht vor Inbetriebnahme, siehe [02 – Pflichtenheft](02-pflichtenheft.md) NFA-DS-04 |

## 7 Handelsrechtliche Nachweispflichten

| Norm | Anforderung | Umsetzung |
|---|---|---|
| § 238 HGB | Buchführungsgrundsätze | IDVs mit Rechnungslegungsbezug werden separat gekennzeichnet |
| § 239 HGB | Ordnungsmäßigkeit / Unveränderlichkeit | `idv_history` append-only |
| § 257 HGB | Aufbewahrungsfrist 10 Jahre | Empfohlene Backup-Strategie |
| § 147 AO | Steuerliche Aufbewahrung | identisch mit HGB |
| GoBD | Grundsätze ordnungsmäßiger Buchführung | Audit-Trail, Zeitstempel, unveränderliche History |

## 8 Zusammenfassung der Compliance-Bewertung

| Anforderung | Status | Bemerkung |
|---|:---:|---|
| MaRisk AT 7.2 – IDV-Register | ✅ | Vollständig abgebildet |
| MaRisk AT 7.2 – Test-/Freigabeverfahren | ✅ | 5 Schritte, 3 Phasen (inkl. revisionssicherer Archivierung der Originaldatei) |
| MaRisk AT 7.2 – Funktionstrennung | ✅ | Entwickler ≠ Freigebender |
| MaRisk AT 7.2 – Regelmäßige Prüfungen | ✅ | Prüfintervalle, Fälligkeits-Dashboard |
| MaRisk AT 7.2 – Änderungsverfolgung | ✅ | `idv_history` |
| BAIT Kap. 4 – Benutzerberechtigungen | ✅ | Rollenmodell, LDAP |
| BAIT Kap. 10 – IDV | ✅ | Kernfunktionalität |
| DORA Art. 8 – Kritische Funktionen | ✅ | DORA-Kritikalitäts-Feld |
| DORA Art. 17 – Incident Management | ⚠️ | Prozess dokumentiert, keine automatische Meldung |
| ISO 27001 A.8.5 – Authentifizierung | ⚠️ | SHA-256 ohne Salt → Migration geplant |
| ISO 27001 A.8.24 – Kryptografie | ⚠️ | SMTP-Passwort unverschlüsselt → Migration geplant |
| DSGVO – Auftragsverarbeitung | ✅ | Keine externe Verarbeitung |
| DSGVO – DSFA | ⚠️ | Durch Bank vor Go-Live durchzuführen |
| HGB § 239 – Unveränderlichkeit | ✅ | Append-only History |

**Legende:** ✅ konform · ⚠️ Restrisiko / Handlungsbedarf

Die mit ⚠️ gekennzeichneten Punkte sind in
[09 – Schwachstellenanalyse](09-schwachstellenanalyse.md) mit
Remediation-Plan und Verantwortlichkeiten hinterlegt.

## 9 Nachweisartefakte für Prüfer

Die folgenden Artefakte können im Rahmen einer Prüfung direkt aus idvault
bereitgestellt werden:

| Artefakt | Bezugsquelle | Format |
|---|---|---|
| Grundgesamtheit Eigenentwicklungen | `/eigenentwicklung/export/excel` | XLSX |
| Prüfungsübersicht | Administrationsbereich | XLSX |
| Maßnahmenübersicht | Administrationsbereich | XLSX |
| Login-Protokoll | `/admin/login-log` + Download | Textdatei |
| Anwendungslog | `/admin/update/log` | Textdatei |
| Änderungshistorie einer IDV | IDV-Detailseite → History-Abschnitt | HTML + Export |
| Rollen-/Berechtigungsmatrix | `docs/01-anwendungsdokumentation.md` | Markdown |
| Sicherheitskonzept | `docs/05-sicherheitskonzept.md` | Markdown |
| Schwachstellenanalyse | `docs/09-schwachstellenanalyse.md` | Markdown |
| Datenmodell-Beschreibung | `docs/04-datenmodell.md` + `schema.sql` | Markdown + SQL |
| Pflichtenheft | `docs/02-pflichtenheft.md` | Markdown |

## 10 Prüfungsvorbereitung

Empfohlene Vorbereitung durch den Auftraggeber vor einer aufsichtlichen
Prüfung:

1. Aktuellsten Stand dieser Dokumentation ausdrucken und dem Prüfer bereitstellen
2. Test-IDV aller Zustände (Entwurf, Genehmigt, Mit Auflagen, Archiviert) demonstrieren
3. Mindestens eine vollständig durchlaufene Prüfung inkl. Maßnahme vorzeigen
4. Beispielhaft ein vierstufiges Freigabeverfahren mit Nachweisen bereitstellen
5. Login-Log-Auszug der vergangenen 30 Tage
6. Export der IDV-Grundgesamtheit
7. Liste der aktuellen Benutzerrollen und -zuordnungen
8. Stand der aktuellen Version und Changelog (`version.json`)
9. Backup- und Restore-Test-Protokoll der letzten 6 Monate
10. Ergebnis des letzten externen Penetrationstests

## 11 Dokumentenfreigabe

Diese Compliance-Dokumentation wird freigegeben durch:

| Rolle | Name | Datum | Unterschrift |
|---|---|---|---|
| Geschäftsleitung | | | |
| IT-Leitung | | | |
| Informationssicherheitsbeauftragter | | | |
| Interne Revision | | | |
| Datenschutzbeauftragter | | | |
| Compliance-Beauftragter | | | |
