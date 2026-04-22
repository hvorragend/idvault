# 01 – Anwendungsdokumentation

**idvault – Register für Individuelle Datenverarbeitungen**

---

## 1 Zweck und fachlicher Kontext

idvault ist eine bankfachliche Anwendung zur vollständigen, regulatorisch
konformen Erfassung, Klassifizierung, Dokumentation und Überwachung von
**Individuellen Datenverarbeitungen (IDV)** im Sinne der
**MaRisk AT 7.2 Tz. 7** (Bankaufsichtliche Mindestanforderungen an das
Risikomanagement) sowie der **BAIT-Orientierungshilfe** der BaFin.

Als IDV gilt jede von einer Fachabteilung oder einzelnem Mitarbeiter
entwickelte und betriebene Anwendung, Berechnung, Auswertung oder
Datenverarbeitungsroutine, die **nicht durch die zentrale IT** entwickelt,
freigegeben und betrieben wird. Typische Beispiele sind Excel-Arbeitsmappen
mit Makros, Access-Datenbanken, Python- oder R-Skripte, Power-BI-Berichte
und SQL-Abfragen.

Darüber hinaus bildet idvault die Anforderungen aus der **Digital
Operational Resilience Act (DORA, Verordnung (EU) 2022/2554)** für die
Identifikation kritischer oder wichtiger Funktionen und die damit
verbundenen IKT-Abhängigkeiten ab.

## 2 Zielgruppen der Anwendung

| Zielgruppe | Nutzung |
|---|---|
| **Fachbereiche** (Kredit, Vertrieb, Marktfolge, Meldewesen, Controlling) | Selbstregistrierung und Pflege eigener IDVs |
| **IDV-Koordinator** | Zentrale Verantwortung für Vollständigkeit und Qualität des IDV-Registers |
| **IDV-Administrator** | Technische Administration, Stammdatenpflege, LDAP, Scanner |
| **Interne Revision** | Lesender Zugriff zur prüferischen Durchsicht |
| **IT-Sicherheit / ISB** | Lesender Zugriff zur Risikobewertung |
| **Geschäftsleitung** | Dashboard-Sicht auf Risikolage |

## 3 Benutzer- und Berechtigungskonzept

### 3.1 Rollen

idvault kennt sechs Rollen. Die Rollenzuweisung erfolgt entweder manuell
durch den IDV-Administrator oder automatisiert über das
LDAP-Gruppen-Rollen-Mapping. Jede Person erhält **genau eine** Rolle; die
IDV-spezifische Zuweisung als Entwickler, Fachverantwortlicher,
Koordinator oder Stellvertreter erfolgt zusätzlich pro IDV im
Erfassungsformular (siehe 3.3).

| Rolle | Zweck | Typischer Zugriff |
|---|---|---|
| **IDV-Administrator** | Systemadministration | Vollzugriff, alle Module |
| **IDV-Koordinator** | Zentrale Registerführung | Schreibzugriff auf alle IDVs |
| **Fachverantwortlicher** | Pflege eigener IDVs | Schreibzugriff auf IDVs, bei denen die Person als Fachverantwortlicher, Entwickler, Koordinator oder Stellvertreter eingetragen ist |
| **IDV-Entwickler** | Technische Umsetzung einer IDV | Wie Fachverantwortlicher, jedoch im Freigabeverfahren der betroffenen IDV von Abschluss-/Ablehnungshandlungen ausgeschlossen (Funktionstrennung, siehe 3.4) |
| **Revision** | Prüferischer Lese-Zugriff | Lesezugriff auf alle IDVs |
| **IT-Sicherheit** | IT-Risikobewertung | Lesezugriff auf alle IDVs |

> Hinweis: Die Rolle `IDV-Entwickler` (als Session-Rolle) existiert zusätzlich
> zur IDV-bezogenen Zuweisung `idv_entwickler_id`. Die Funktionstrennung
> (3.4) wertet ausschließlich `idv_entwickler_id` auf dem betroffenen IDV
> aus, nicht die Session-Rolle.

### 3.2 Berechtigungsmatrix

Legende: ✓ = erlaubt · — = nicht erlaubt · (eigene) = nur für IDVs, bei
denen die Person als Fachverantwortlicher, Entwickler, Koordinator oder
Stellvertreter eingetragen ist · (zugewiesen) = nur für die im
Freigabeschritt zugewiesene Person, deren aktiven Stellvertreter oder
Mitglieder des zugewiesenen Pools (Admin jederzeit).

| Funktion | Admin | Koordinator | Fachverantw. | Entwickler | Revision | IT-Sicherheit |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Dashboard anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| IDV-Liste anzeigen (alle) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| IDV-Detail anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| IDV anlegen | ✓ | ✓ | ✓ | ✓ | — | — |
| IDV bearbeiten | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Neue IDV-Version anlegen | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| IDV-Status ändern (Entwurf → In Prüfung → Genehmigt) | ✓ | ✓ | — | — | — | — |
| Teststatus ändern | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Bulk-Status ändern | ✓ | ✓ | — | — | — | — |
| IDV löschen (Bulk) | ✓ | — | — | — | — | — |
| Prüfungen anlegen | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Prüfungen bearbeiten / löschen | ✓ | — | — | — | — | — |
| Prüfungen anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Maßnahmen anlegen / als erledigt markieren | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Maßnahmen bearbeiten / löschen | ✓ | — | — | — | — | — |
| Maßnahmen anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Testfälle / Technische Tests anlegen/bearbeiten | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Freigabeverfahren Phase 1 starten | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Freigabeverfahren Phase 2 starten | ✓ | ✓ | ✓ (eigene) | ✓ (eigene) | — | — |
| Freigabe-Schritt abschließen / ablehnen | ✓ | ✓ (zugewiesen) | ✓ (zugewiesen) | ✓ (zugewiesen, nicht als Entwickler der IDV) | — | — |
| Archivierungs-Schritt (Phase 3) durchführen | ✓ | ✓ (zugewiesen) | ✓ (zugewiesen) | ✓ (zugewiesen, nicht als Entwickler der IDV) | — | — |
| Freigabe-Schritt wieder öffnen | ✓ | — | — | — | — | — |
| Freigabeverfahren abbrechen | ✓ | — | — | — | — | — |
| Scanner-Funde anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Scanner-Funde zuordnen (Quick-Assign / Bulk) | ✓ | ✓ | ✓ | ✓ | — | — |
| Scanner-Funde ignorieren / reaktivieren | ✓ | ✓ | — | — | — | — |
| Scanner-Funde löschen | ✓ | — | — | — | — | — |
| Scan starten | ✓ | — | — | — | — | — |
| IDV aus Scannerfund registrieren | ✓ | ✓ | — | — | — | — |
| Cognos-Berichte importieren / als IDV registrieren | ✓ | ✓ | — | — | — | — |
| Excel-Export | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Administration (Stammdaten anlegen/bearbeiten) | ✓ | — | — | — | — | — |
| Stammdaten löschen | ✓ | — | — | — | — | — |
| Freigabe-Pools verwalten | ✓ | — | — | — | — | — |
| E-Mail-Einstellungen (SMTP) | ✓ | — | — | — | — | — |
| Mitarbeiter-Import (CSV / LDAP) | ✓ | — | — | — | — | — |
| LDAP konfigurieren / Gruppen-Mapping | ✓ | — | — | — | — | — |
| Software-Update einspielen | ✓ | — | — | — | — | — |
| Notfall-Zugang aktivieren | ✓ | — | — | — | — | — |

Durchgesetzt wird die Matrix durch die Decorator-Funktionen
`login_required`, `admin_required`, `write_access_required` (nur Admin +
Koordinator) und `own_write_required` (Admin, Koordinator,
Fachverantwortlicher, Entwickler) in `webapp/routes/__init__.py` sowie die
Row-Level-Guards `user_can_read_idv` / `user_can_write_idv` in
`webapp/security.py`.

### 3.3 Sichtbarkeit von IDVs und Schreibzugriff (Row-Level Security)

**Lesezugriff auf IDV-Listen und -Details** (`can_read_all()` bzw.
`user_can_read_idv()`):

```
Admin · Koordinator · Revision · IT-Sicherheit · Fachverantwortlicher · IDV-Entwickler
    → sehen ALLE IDVs des Registers
```

Der Lesezugriff ist für alle produktiven Rollen "read-all". Einschränkungen
ergeben sich ausschließlich beim **Schreibzugriff**.

**Schreibzugriff** (`user_can_write_idv()`):

```
Admin · Koordinator                                → dürfen jede IDV bearbeiten

Fachverantwortlicher · IDV-Entwickler              → dürfen nur IDVs bearbeiten,
                                                     bei denen die eigene Person-ID
                                                     in mindestens einem der Felder
                                                     steht:
                                                        fachverantwortlicher_id
                                                        idv_entwickler_id
                                                        idv_koordinator_id
                                                        stellvertreter_id

Revision · IT-Sicherheit                           → kein Schreibzugriff
```

**Zugriff auf Freigabeschritte** (`_can_complete_schritt()`):

Ein Schritt darf von genau einer der folgenden Personen abgeschlossen
oder abgelehnt werden:

- der im Schritt hinterlegten zugewiesenen Person (`zugewiesen_an_id`),
- ihrem aktiven Stellvertreter (`persons.stellvertreter_id`, nur solange
  `abwesend_bis` in der Zukunft liegt),
- Mitgliedern des dem Schritt zugewiesenen **Freigabe-Pools** (`pool_id`,
  siehe 5.5.1),
- einem IDV-Administrator (jederzeit, Funktionstrennung übersteuert,
  siehe 3.4).

### 3.4 Funktionstrennung (Segregation of Duties)

Im Test- und Freigabeverfahren (Phasen 1–3) darf eine Person, die auf der
betroffenen IDV als **IDV-Entwickler** (`idv_entwickler_id`) eingetragen
ist, keinen Freigabeschritt abschließen oder ablehnen (Vier-Augen-Prinzip).
Die Prüfung erfolgt vor jedem Abschluss in
`webapp/routes/freigaben.py:_funktionstrennung_ok()`.

**Administrator-Ausnahme:** Ein IDV-Administrator kann bei
organisatorischem Bedarf einspringen und auch dann Freigabeschritte
abschließen, wenn er als Entwickler auf der betroffenen IDV eingetragen
ist. Der Eingriff wird in `idv_history` mit der Aktion
`freigabe_schritt_erledigt` und der ausführenden Person protokolliert;
ein gesondertes Override-Flag wird derzeit nicht gesetzt, das
Zusammenwirken von Session-Rolle `IDV-Administrator` und eigener
Eintragung als `idv_entwickler_id` ist jedoch aus der History
rekonstruierbar.

## 4 Authentifizierungsverfahren

### 4.1 Produktiv-Authentifizierung

| Methode | Beschreibung |
|---|---|
| **LDAP / Active Directory** | Primäre Methode für Bankumgebungen; Bind über LDAPS (Port 636) |
| **Lokal (Datenbank)** | Fallback-Methode mit gehashten Passwörtern |
| **Notfall-Zugang** | Manuell aktivierbar; umgeht LDAP vollständig |

### 4.2 LDAP-Login-Ablauf

```
1. Benutzer gibt AD-Anmeldename + Windows-Passwort ein
2. idvault verbindet per LDAPS mit dem konfigurierten Server
3. Service-Account sucht den Benutzer per sAMAccountName
4. LDAP-Bind mit dem gefundenen User-DN + eingegebenem Passwort
   (das Passwort verlässt idvault nie im Klartext)
5. Bei Erfolg: Gruppen-Mitgliedschaften auslesen (memberOf)
6. Gruppen-DNs mit dem Mapping abgleichen → idvault-Rolle bestimmen
7. Person in idvault automatisch anlegen oder aktualisieren (JIT Provisioning)
8. Session setzen, weiterleiten zum Dashboard
```

### 4.3 Automatischer Fallback

Ist der LDAP-Server nicht erreichbar, wechselt idvault automatisch auf den
lokalen Login. Dieser greift auf in der Datenbank hinterlegte Passwort-Hashes
zurück. Die Umschaltung erfolgt **ohne Konfigurationsänderung** und wird
im Login-Log vermerkt.

### 4.4 Demo-Fallback (Erstinstallation)

Für die Erstinstallation sind folgende Demo-Zugänge hinterlegt. Diese
**müssen** vor produktivem Einsatz deaktiviert werden (vgl.
[05 – Sicherheitskonzept](05-sicherheitskonzept.md) Abschnitt 7).

| Benutzername | Passwort | Rolle |
|---|---|---|
| `admin` | `idvault2026` | IDV-Administrator |
| `koordinator` | `demo` | IDV-Koordinator |
| `fachverantwortlicher` | `demo` | Fachverantwortlicher |

---

## 5 Funktionsmodule

### 5.1 Dashboard

Einstiegsseite nach dem Login. Liefert einen zielgruppenabhängigen
Überblick über den IDV-Bestand:

- Anzahl aktiver IDVs nach Status (Entwurf, In Prüfung, Genehmigt)
- Kritische IDVs (GDA = 4, steuerungsrelevant, DORA-kritisch/wichtig)
- Überfällige und bald fällige Prüfungen
- Offene Maßnahmen mit Eskalationsstatus
- Scanner-Eingangszähler (neue, noch nicht klassifizierte Dateien)

### 5.2 IDV-Grundgesamtheit (IDV-Register)

Kernbestandteil der Anwendung. Verwaltet die vollständige Grundgesamtheit
aller registrierten IDVs.

#### 5.2.1 Filter- und Suchfunktionen

- Freitextsuche über Bezeichnung, IDV-ID, Beschreibung
- Filter nach Status, GDA-Wert, IDV-Typ, Plattform, Organisationseinheit
- Compliance-Filter: DORA-kritisch, steuerungsrelevant, unvollständig
- Paginierung und Sortierung aller Listenansichten

#### 5.2.2 IDV-Erfassungsformular

Das Formular gliedert sich in fünf Abschnitte:

1. **Stammdaten** – Bezeichnung, IDV-Typ, Version, Kurzbeschreibung
2. **Wesentlichkeitsbeurteilung** – Steuerungsrelevanz, Rechnungslegungsrelevanz, DORA-Kritikalität
3. **Risikobewertung** – Risikoklasse, Verfügbarkeit, Integrität, Vertraulichkeit (CIA-Triade)
4. **Technik & Betrieb** – Plattform, Nutzungsfrequenz, Zugriffsschutz, Makros
5. **Verantwortliche** – Organisationseinheit, Fachverantwortlicher, Entwickler, Koordinator, Stellvertreter

#### 5.2.3 Versionierung

Über *"Neue Version erstellen"* wird eine Kopie der IDV mit neuer Versionsnummer
angelegt:

- `teststatus` wird auf `Wertung ausstehend` zurückgesetzt
- `letzte_aenderungsart` wird auf `wesentlich` oder `unwesentlich` gesetzt
- Die alte IDV wird als `vorgaenger_idv_id` verknüpft
- Bei `letzte_aenderungsart = 'unwesentlich'` entfällt das Freigabeverfahren

### 5.3 Prüfungen

Dokumentation regelmäßiger und anlassbezogener Prüfungen gemäß
MaRisk AT 7.2 Tz. 7. Eine Prüfung enthält:

| Feld | Inhalt |
|---|---|
| Prüfungsart | Erstprüfung, Regelprüfung, Anlassprüfung, Revisionsprüfung |
| Prüfungsdatum | Datum der Durchführung |
| Prüfer | Person aus dem Personenkatalog |
| Ergebnis | Ohne Befund / Mit Befund / Kritischer Befund / Nicht bestanden |
| Befundbeschreibung | Freitext zu festgestellten Mängeln |
| Nächste Prüfung | Datum – wird automatisch ins IDV-Register übernommen |
| Kommentar | Interne Anmerkungen |

Nach dem Speichern wird `naechste_pruefung` im IDV-Register aktualisiert
und der Prüfstatus neu berechnet.

### 5.4 Maßnahmen

Aus Prüfungsbefunden oder proaktiver Risikobewertung abgeleitete Remediation-
Maßnahmen. Jede Maßnahme enthält:

| Feld | Inhalt |
|---|---|
| Titel | Kurze Beschreibung |
| Beschreibung | Detaillierte Erläuterung |
| Maßnahmentyp | z. B. Dokumentation, Zugriffsschutz, Ablösung |
| Priorität | Kritisch / Hoch / Mittel / Niedrig |
| Verantwortlicher | Person aus dem Personenkatalog |
| Fälligkeitsdatum | Zieldatum |
| Status | Offen → In Bearbeitung → Erledigt |

### 5.5 Test- und Freigabeverfahren

Für **wesentliche IDVs** mit wesentlicher Änderung ist ein fünfstufiges
Test-, Abnahme- und Archivierungsverfahren in drei Phasen vorgesehen:

```
Phase 1 (parallel):         Phase 2 (parallel):         Phase 3:
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────────┐
│ Fachlicher Test  │        │ Fachliche Abnahme│        │ Archivierung         │
└────────┬─────────┘        └────────┬─────────┘        │ Originaldatei        │
         │ beide bestanden?          │ beide bestanden? │ (revisionssicher)    │
┌────────┴─────────┐    →   ┌────────┴─────────┐    →   └──────────┬───────────┘
│Technischer Test  │        │Technische Abnahme│                   │
└──────────────────┘        └──────────────────┘                   ▼
                                                          IDV freigegeben
```

**Phase 2 kann erst gestartet werden, wenn beide Phase-1-Schritte bestanden sind.**
Phase 3 wird automatisch angelegt, sobald beide Phase-2-Schritte bestanden
sind; die Gesamtfreigabe erfolgt erst nach Abschluss der Archivierung.

| Schritt-Status | Bedeutung |
|---|---|
| Ausstehend | Schritt angelegt, wartet auf Durchführung |
| Bestanden | Erfolgreich abgeschlossen |
| Nicht bestanden | Abgelehnt mit Befunden → Teststatus zurück auf `In Bearbeitung` |
| Abgebrochen | Durch Administrator abgebrochen |

Nachweise (Screenshots, Testberichte, Freigabeerklärungen) können pro
Schritt als Datei-Upload (PDF, XLSX, DOCX u. a., max. 32 MB) hinterlegt werden.

#### 5.5.1 Zuweisung eines Schritts: Person, Stellvertreter oder Pool

Beim Starten einer Phase wird jedem Schritt entweder eine Person oder ein
**Freigabe-Pool** zugewiesen. Pools werden im Administrationsbereich unter
*Freigabe-Pools* gepflegt und enthalten eine Menge berechtigter Personen.

| Zuweisung | Wer darf den Schritt abschließen / ablehnen |
|---|---|
| Einzelperson (`zugewiesen_an_id`) | Die zugewiesene Person, bei aktueller Abwesenheit zusätzlich ihr hinterlegter Stellvertreter |
| Pool (`pool_id`) | Jedes aktive Mitglied des Pools |

Administratoren können jederzeit eingreifen; Entwickler der betroffenen
IDV sind unabhängig von der Zuweisung gesperrt (siehe 3.4).

#### 5.5.2 Administrative Eingriffe

| Aktion | Berechtigte Rolle | Effekt |
|---|---|---|
| Freigabeverfahren abbrechen | Nur IDV-Administrator | Alle offenen Schritte werden auf `Abgebrochen` gesetzt; Teststatus geht zurück auf `In Bearbeitung` |
| Einzelnen Schritt wieder öffnen | Nur IDV-Administrator | Ein bereits abgeschlossener oder abgelehnter Schritt wird auf `Ausstehend` zurückgesetzt |

**Phase 3 – Archivierung der Originaldatei (MaRisk AT 7.2 / HGB § 239):**
Die Originaldatei einer wesentlichen Eigenentwicklung muss revisionssicher
archiviert werden. Der Schritt bietet drei Abschlusspfade:

| Pfad | Verhalten |
|---|---|
| **Scanner-Datei übernehmen** | Übernahme einer bereits gescannten Datei aus dem Quell-Netzlaufwerk. Die mit der IDV verknüpften Scanner-Funde (`idv_register.file_id` + `idv_file_links`) werden zur Auswahl angeboten. Die Datei wird vom Originalpfad in das Archiv kopiert; die SHA-256-Prüfsumme wird dabei neu berechnet. Voraussetzung: Lesezugriff auf den Originalpfad. |
| **Datei manuell hochladen** | Upload der Originaldatei (bis 256 MB, beliebiges Format) über den Browser. |
| **Nicht verfügbar** | Für Fälle, in denen die Datei selbst nicht exportierbar ist (z.B. Cognos-Berichte in agree21Analysen, serverseitige Skripte ohne Sicherung). Es ist eine nachvollziehbare Begründung zwingend erforderlich; der Schritt wird als dokumentierter Statusschritt festgehalten. |

In allen Datei-Pfaden wird die Archivdatei schreibgeschützt unter
`<instance_path>/uploads/archiv/<idv_db_id>/<YYYYMMDD_HHMMSS>_<dateiname>`
abgelegt und mit einer SHA-256-Prüfsumme versehen. Der Download einer
archivierten Originaldatei und der Zugriff auf die SHA-256-Prüfsumme sind
in der Detailansicht der IDV möglich.

### 5.6 Genehmigungen (4-Augen-Workflow)

Separater, vom Test-/Freigabeverfahren unabhängiger Genehmigungsprozess:

| Genehmigungsstufe | Zuständig | Pflicht |
|---|---|---|
| **Stufe 1** | IDV-Koordinator | Immer |
| **Stufe 2** | IT-Sicherheit / Revision | Nur bei GDA = 4 oder DORA-kritisch/wichtig |

Genehmigungsarten: Erstfreigabe, Wiederfreigabe, Wesentliche Änderung, Ablösung.

### 5.7 Scanner-Funde

Das Scanner-Modul umfasst zwei getrennte Scanner:

- **Dateisystem-Scanner** für Netzlaufwerke und UNC-Pfade
- **Teams-Scanner** für Microsoft Teams / SharePoint (Microsoft Graph API)

Beide Scanner legen ihre Funde in der gemeinsamen Datenbank ab. Die
Funde durchlaufen folgenden Lebenszyklus:

```
Neu → Zur Registrierung → Registriert
 │                              ↑
 ├── direkt registrieren ───────┘
 └── Ignoriert
```

Details siehe [10 – Scanner](10-scanner.md).

### 5.8 Administration

- **Personen** – Mitarbeiterverwaltung (inkl. User-ID, E-Mail, AD-Name, Passwort)
- **Organisationseinheiten** – Abteilungs- und Bereichsstruktur
- **Geschäftsprozesse** – Prozesskatalog mit Kritikalitätskennzeichnung
- **Plattformen** – Technologiekatalog (Excel, Python, Power BI …)
- **Klassifizierungen** – Konfigurierbare Enum-Werte
- **Wesentlichkeitskriterien** – Konfigurierbarer Fragebogen
- **E-Mail-Einstellungen** – SMTP-Konfiguration
- **LDAP / Active Directory** – Server-Konfiguration und Gruppen-Mapping
- **Scanner-Einstellungen** – Scan-Pfade, Dateitypen, Ausschlüsse
- **Software-Update** – Einspielen neuer Versionen ohne EXE-Austausch

### 5.9 Reports und Export

- **Excel-Export** der gesamten IDV-Grundgesamtheit (*.xlsx*)
- **Listenexporte** aller Prüfungen, Maßnahmen, Genehmigungen
- **Log-Export** (Login-Log, App-Log) für Revision und Forensik

## 6 Workflows

### 6.1 Gesamtablauf einer IDV

```
1.  Scanner läuft (wöchentlich per Scheduled Task)
        ↓
2.  Eingang sichten → "Zur Registrierung vormerken" oder ignorieren
        ↓
3.  Vorgemerkte Dateien → "Als IDV registrieren"
        ↓
4.  IDV-Formular ausfüllen (Wesentlichkeit, Klassifizierung, Verantwortliche)
        ↓
5.  Status: Entwurf → In Prüfung → Genehmigt
        ↓
6.  Bei wesentlicher IDV: Test- und Freigabeverfahren (4 Schritte)
        ↓
7.  Regelprüfung fällig (nach pruefintervall_monate)
        ↓
8.  Prüfung dokumentieren → Ergebnis + nächstes Prüfdatum
        ↓
9.  Bei Befund: Maßnahme anlegen → verfolgen bis Erledigt
        ↓
10. Dashboard zeigt Gesamtstatus jederzeit aktuell
```

### 6.2 Status-Workflow IDV

```
Entwurf → In Prüfung → Genehmigt
             │              │
             ▼              ▼
         Abgelehnt    Abgekündigt → Archiviert

Genehmigt mit Auflagen → Genehmigt
```

Jeder Statuswechsel erzeugt einen History-Eintrag
(`idv_history`-Tabelle) mit Zeitstempel und ausführendem Benutzer.

### 6.3 Teststatus

Parallel zum IDV-Status geführt; bildet den Fortschritt im
Test- und Freigabeverfahren ab:

```
Wertung ausstehend → In Bearbeitung → Freigabe ausstehend → Freigegeben
                           ↑                    │
                           └── bei Ablehnung ────┘
                           └── bei Abbruch ──────┘
```

### 6.4 Maßnahmen-Workflow

```
Offen → In Bearbeitung → Erledigt
  │
  └── Zurückgestellt
```

## 7 Automatische Benachrichtigungen

idvault versendet ereignisgesteuert E-Mails via SMTP. Empfänger
werden aus der Personen-Tabelle abgeleitet.

| Ereignis | Empfänger |
|---|---|
| Neue Datei im Scanner erkannt | IDV-Koordinatoren und Administratoren |
| Prüfung überfällig | Fachverantwortlicher der IDV |
| Maßnahme überfällig | Verantwortlicher der Maßnahme |
| Freigabeverfahren gestartet (Phase 1/2) | Zugewiesene Prüfer + Koordinatoren |
| Freigabeverfahren vollständig bestanden | Koordinatoren, Administratoren, Entwickler |
| Datei-Bewertung | Verantwortlicher |

Die Konfiguration erfolgt ausschließlich unter `Administration →
E-Mail-Einstellungen (SMTP)`. Die Werte werden in der SQLite-Tabelle
`app_settings` abgelegt; das SMTP-Passwort wird Fernet-verschlüsselt
gespeichert.

## 8 Versions- und Änderungshistorie

Alle inhaltlichen Änderungen an einer IDV werden in `idv_history`
protokolliert:

- Aktion (`erstellt`, `geaendert`, `status_geaendert`, `geprueft`)
- Geänderte Felder (JSON-Delta)
- Ausführender Benutzer
- Zeitstempel (ISO 8601 UTC)

Die History ist **append-only** und bildet damit einen vollständigen,
nicht-rekonstruierbaren Audit-Trail (vgl. [05 – Sicherheitskonzept](05-sicherheitskonzept.md)).

## 9 Datenexport und Nachweis

Für die Nachweisführung gegenüber Revision und Aufsicht stehen folgende
Exporte zur Verfügung:

| Export | Inhalt | Format |
|---|---|---|
| IDV-Grundgesamtheit | Vollständige Liste aller IDVs | XLSX |
| Prüfungsübersicht | Alle Prüfungen mit Ergebnis und Befunden | XLSX |
| Maßnahmenübersicht | Alle Maßnahmen mit Status und Fälligkeiten | XLSX |
| Login-Log | Authentifizierungsprotokoll | Textdatei |
| Anwendungs-Log | Anwendungsereignisse (WARN/ERROR) | Textdatei |

## 10 Sprache, Zeitzone und Formate

| Parameter | Wert |
|---|---|
| Anwendungssprache | Deutsch (de-DE) |
| Datums-/Zeitformat (Speicherung) | ISO 8601, UTC |
| Datums-/Zeitformat (Anzeige) | `TT.MM.JJJJ` / `TT.MM.JJJJ HH:MM` (Europe/Berlin) |
| Zahlformat | Deutsche Konvention (`1.234,56`) |
| Zeichencodierung | UTF-8 |

## 11 Hilfe und Support

- Alle Formulare enthalten kontextbezogene Erläuterungstexte
- Diese Dokumentation ist in der Anwendung unter *System → Dokumentation* verlinkt
- Log-Downloads für den First- und Second-Level-Support
- Ansprechpartner: IDV-Koordinator der Bank (intern)
