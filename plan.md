# IDV-Register: Konzept & Umsetzungsplan

## 1. Einordnung & Zielbild

Du brauchst im Kern drei Dinge:

- **Grundgesamtheit** (Discovery): automatisiertes, regelmäßiges Scannen aller Netzlaufwerke → vollständige Dateiliste mit Metadaten & Hash
- **Klassifizierung** (Classification): regelbasierte + manuelle Einordnung nach IDV-Kriterien (MaRisk AT 7.2, DORA Art. 28/30)
- **Register** (Registry): zentrale, revisionssichere Ablage mit Workflow für Ersterfassung, Änderung, Abkündigung

---

## 2. Welche Dateitypen scannen?

| Kategorie | Dateierweiterungen |
|---|---|
| Excel (Makros/Formeln) | `.xls`, `.xlsx`, `.xlsm`, `.xlsb`, `.xltm` |
| Access-Datenbanken | `.accdb`, `.mdb`, `.accde` |
| IDV-spezifisch | `.ida`, `.idv` |
| VBA-Projekte | `.bas`, `.cls`, `.frm` |
| Python/R-Skripte | `.py`, `.r`, `.rmd` |
| SQL-Skripte | `.sql` |
| Power BI | `.pbix`, `.pbit` |
| Sonstige Office-Makros | `.dotm`, `.pptm` |

Nicht alles davon ist automatisch IDV – aber alle sind Kandidaten für die Grundgesamtheit.

---

## 3. Empfohlene Architektur

Ich empfehle dir eine **dreigleisige Lösung**, die sich mit M365 gut verträgt:

### Schicht 1 – Scanner (Python-Skript, geplant als Task)
- Läuft als Windows Scheduled Task oder als PowerShell/Python auf einem Server
- Scannt UNC-Pfade (`\\server\share\...`) rekursiv
- Erhebt Metadaten + **SHA-256-Hash** (eindeutige Fingerabdrücke, erkennt auch Kopien)
- Schreibt Ergebnisse in eine **SQLite-Datenbank**

### Schicht 2 – Register-Datenbank (SQLite)
Zwei Kerntabellen:

```
scan_results      → Rohdaten jedes Scans (Snapshot)
idv_register      → Das eigentliche IDV-Register (kuratiert, mit Klassifizierung)
```

### Schicht 3 – Frontend/UI
Hier hast du zwei sinnvolle Optionen für M365:

**Option A – SharePoint + Power Apps + Power Automate** *(empfohlen für Volksbank-Umfeld)*
- IDV-Register als SharePoint-Liste
- Power Apps als Erfassungsmaske (Klassifizierung, Verantwortliche, GDA-Bewertung)
- Power Automate für Workflows (Freigabe, Änderungsbenachrichtigung, Wiedervorlage)
- Power BI für Auswertungen/Dashboards
- Vorteil: kein eigener Server nötig, Zugriffsrechte via M365, revisionssichere Versionierung

**Option B – Flask-Webapp + SQLite** *(Eigenentwicklung, für mehr Kontrolle)*
- Läuft auf einem Proxmox-LXC bei dir
- Volle Kontrolle, kein M365-Lizenzaufwand
- Nachteil: Pflege, Backup, Updates selbst

---

## 4. Metadaten pro Datei

Der Scanner sollte folgendes erheben:

| Attribut | Quelle |
|---|---|
| Vollständiger Pfad | Filesystem |
| Dateiname | Filesystem |
| Erstelldatum | Filesystem (`st_ctime`) |
| Änderungsdatum | Filesystem (`st_mtime`) |
| Dateigröße (Bytes) | Filesystem |
| SHA-256-Hash | Dateiinhalt |
| Dateityp/Extension | Filesystem |
| Besitzer/Owner | Windows ACL (`win32security`) |
| Letzter Autor | Office-Metadaten (OOXML: `<dc:creator>`, `<cp:lastModifiedBy>`) |
| Hat Makros/VBA | OOXML-Analyse (`xl/vbaProject.bin` vorhanden?) |
| Externe Verknüpfungen | OOXML-Analyse |
| Anzahl Tabellenblätter | OOXML-Analyse |
| Scan-Zeitstempel | Scanner |
| Server/Share | Konfiguration |

---

## 5. Klassifizierungskriterien im Register

Für jede identifizierte IDV-Eigenentwicklung werden dann manuell/halbautomatisch bewertet:

- **Steuerungsrelevanz** (ja/nein + Begründung)
- **Rechnungslegungsrelevanz** (ja/nein + Begründung)
- **Zuordnung Geschäftsprozess** (GP-Nummer, GP-Name)
- **GDA-Bewertung** (1–4, mit 4 = vollständige Abhängigkeit)
- **Kritikalitätseinstufung** (kritisch/wichtig nach DORA-Logik)
- **Verantwortlicher Fachbereich**
- **IDV-Verantwortlicher** (Person)
- **Freigabestatus** (in Prüfung / freigegeben / abgekündigt)
- **Prüfintervall / nächste Prüfung**
- **Versionsnummer** der IDV-Eigenentwicklung
- **Ablösung geplant?** (ja/nein + Zieldatum)

---

## 6. Umsetzungsschritte

**Phase 1 – Scanner bauen** *(2–3 Tage)*
Python-Skript mit `os.scandir`, `hashlib`, `openpyxl`/`zipfile` für OOXML-Analyse, `win32security` für Owner. Output in SQLite.

**Phase 2 – Ersterfassung** *(1–2 Wochen)*
Scanner gegen alle Laufwerke laufen lassen, Rohliste bereinigen, Duplikate über Hash identifizieren, offensichtliche Nicht-IDV aussortieren.

**Phase 3 – Register aufbauen** *(parallel, 1–2 Wochen)*
SharePoint-Liste oder SQLite-Schema definieren, Power Apps-Maske bauen (oder Flask-UI), Klassifizierungsworkflow mit Fachbereichen abstimmen.

**Phase 4 – Klassifizierung** *(4–8 Wochen, organisatorisch)*
Fachbereiche befragen, GDA-Bewertungen einholen, Verantwortliche benennen.

**Phase 5 – Regelbetrieb** *(dauerhaft)*
Scanner als Scheduled Task wöchentlich/monatlich, Delta-Vergleich (neue Dateien, geänderte Hashes, gelöschte Dateien) → automatische Benachrichtigung bei Änderungen an registrierten IDVs.

---

## 7. Meine Empfehlung für das Zielmedium

Für eine **Volksbank im regulierten Umfeld (MaRisk/DORA)** würde ich so vorgehen:

**Scanner → SQLite** (technische Rohdaten, automatisiert)
**Register → SharePoint-Liste** (kuratierte IDV-Erfassung, Power Apps UI, Workflow via Power Automate)
**Reporting → Power BI** (Dashboard für Vorstand/IT-Revision)

Das gibt dir Revisionssicherheit, M365-Integration (SSO, Berechtigungen, Audit-Log) und du musst keinen eigenen Webserver pflegen. Power Automate übernimmt Wiedervorlagen und Genehmigungsworkflows.

---

## 8. Nächste Schritte

- **Python-Scanner-Skript** ausarbeiten (inkl. OOXML-Analyse, Hash, Owner, SQLite-Output)?
- **SharePoint-Listenstruktur** für das IDV-Register definieren?
- **Power Apps-Datenmodell** skizzieren?
- **Klassifizierungsmatrix** (GDA, Steuerungsrelevanz etc.) als strukturiertes Schema entwerfen?



---------------



## Option A: SharePoint + Power Apps (M365)

**Ja, vollwertige Mehrbenutzerlösung** – direkt aus der Box:

- Jeder M365-Nutzer mit Berechtigung kann die Power App im Browser oder mobil öffnen
- Rollenkonzept über SharePoint-Gruppen: z.B. *IDV-Erfasser*, *Fachverantwortlicher*, *IDV-Administrator*, *Lesend (Revision/Vorstand)*
- Gleichzeitiges Arbeiten mehrerer Nutzer ist kein Problem
- Änderungshistorie und Versionierung durch SharePoint nativ vorhanden
- Power Automate liefert Genehmigungsworkflows mit E-Mail-Benachrichtigung

**Einschränkung:** Die Power Apps-Oberfläche ist gut, aber nicht schön. Für komplexere Masken (z.B. mehrstufige Klassifizierung, GDA-Wizard) stößt du an Grenzen der Low-Code-Plattform. Und: Du bist abhängig von M365-Lizenzen der Nutzer.

---

## Option B: Flask-Webapp (selbst gehostet)

**Ja, ebenfalls Mehrbenutzer** – aber du baust es selbst:

- Läuft in deinem Proxmox-LXC, erreichbar über Nginx Proxy Manager (hast du ja bereits)
- Eigene Benutzerverwaltung oder SSO via Azure AD (M365-Login)
- Volle Kontrolle über UI/UX, keine Low-Code-Grenzen
- SQLite reicht für ~50–100 gleichzeitige Nutzer problemlos; bei mehr → PostgreSQL

**Einschränkung:** Du musst die App bauen und pflegen. Updates, Backup, Security – alles an dir.

---

## Meine ehrliche Empfehlung für deinen Fall

Da du in einer **regulierten Volksbank-Umgebung** arbeitest und M365 bereits habt:

> **Kurzfristig:** SharePoint-Liste + Power Apps – schnell deployed, keine eigene Infrastruktur, Audit-Trail inklusive, Vorstand/Revision kann direkt draufschauen.

> **Mittelfristig**, wenn der Funktionsumfang wächst (komplexe Klassifizierungslogik, Scanner-Integration, Delta-Reports): eine **einfache Flask- oder FastAPI-App** mit Azure AD Login – deployed in deinem LXC, aber mit M365-SSO. Das gibt dir das Beste aus beiden Welten.

