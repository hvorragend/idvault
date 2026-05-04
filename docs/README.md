# idvscope – Dokumentation

Vollständige Dokumentation der idvscope-Anwendung zur Erfassung,
Klassifizierung und Überwachung von **Eigenentwicklungen** (einschließlich
**Individueller Datenverarbeitungen (IDV)** nach **MaRisk AT 7.2**),
**DORA (Art. 28, 30)** und **BAIT**.

> **Begriffsklärung:** „Eigenentwicklung" ist in dieser Anwendung der
> Oberbegriff für alle erfassten Datenverarbeitungen (Arbeitshilfen,
> IDVs, Eigenprogrammierungen, Auftragsprogrammierungen). „IDV"
> bezeichnet ausschließlich das regulatorische Klassifikationsergebnis
> einer Eigenentwicklung nach MaRisk AT 7.2.

Diese Dokumentation ist so strukturiert, dass sie einer **aufsichtsrechtlichen
Prüfung** durch die interne Revision, externe Wirtschaftsprüfer, die
BaFin oder die Deutsche Bundesbank standhält. Die Gliederung folgt den
branchenüblichen Dokumentationsanforderungen für bankfachliche
IT-Anwendungen.

---

## Dokumenten-Struktur

### Fachlich / Anwender

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [01 – Anwendungsdokumentation](01-anwendungsdokumentation.md) | Vollständiger Funktionsumfang, Workflows, Rollen, Bedienung | Fachbereich, Koordinator, Anwender |
| [07 – Aufsichtsrechtliche Konformität](07-aufsichtsrecht.md) | Mapping MaRisk AT 7.2 · DORA · BAIT · MaGo · KAIT | Revision, Compliance, Prüfer |

### Entwicklung / Spezifikation

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [02 – Pflichtenheft](02-pflichtenheft.md) | Funktionale und nicht-funktionale Anforderungen (FA/NFA) | Entwicklung, Auftraggeber |
| [03 – Architektur](03-architektur.md) | Systemarchitektur, Komponenten, Schnittstellen | Architekten, Entwickler, Revision |
| [04 – Datenmodell](04-datenmodell.md) | Datenbankschema, Tabellen, Beziehungen, Views | Entwickler, DBA, Revision |

### Sicherheit / Betrieb

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [05 – Sicherheitskonzept](05-sicherheitskonzept.md) | Authentifizierung, Autorisierung, Verschlüsselung, Audit-Trail | IT-Sicherheit, Revision |
| [06 – Betriebshandbuch](06-betriebshandbuch.md) | Installation, Konfiguration, Monitoring, Backup, Update | Betrieb, Administratoren |

### Qualitätssicherung

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [08 – Quellcodeanalyse](08-quellcodeanalyse.md) | Code-Qualitätsbewertung, Metriken, technische Schulden | Revision, IT-Sicherheit |
| [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md) | CVE-Mapping, OWASP-Top-10, Risikobewertung, Remediation | IT-Sicherheit, Revision |

### Teilsysteme

| Dokument | Inhalt | Zielgruppe |
|---|---|---|
| [10 – Scanner](10-scanner.md) | Scanner für Eigenentwicklungen (Dateisystem, Teams/SharePoint) | Administratoren |
| [11 – Build &amp; Deployment](11-build-deployment.md) | Build-Pipeline, PyInstaller, Sidecar-Updates | Entwicklung, Betrieb |
| [12 – Glossar](12-glossar.md) | Begriffe, Abkürzungen, Rollen | Alle |

---

## Dokumentenhistorie

| Version | Datum | Autor | Änderungen |
|---|---|---|---|
| 1.0.0 | 2026-04-15 | Restrukturierung | Erstfassung strukturierter Dokumentation |

---

## Geltungsbereich

Diese Dokumentation gilt für **idvscope Version 0.1.0** und nachfolgende
Patch-Versionen innerhalb der Hauptversion 0.x, soweit nicht ausdrücklich
abweichende Angaben erfolgen. Die aktuell ausgelieferte Version ist in
`version.json` dokumentiert.

## Bezugsdokumente

- MaRisk (Mindestanforderungen an das Risikomanagement) – BaFin, aktuelle Fassung
- BAIT (Bankaufsichtliche Anforderungen an die IT) – BaFin
- DORA (Digital Operational Resilience Act) – Verordnung (EU) 2022/2554
- MaGo (Mindestanforderungen an die Geschäftsorganisation) – BaFin
- ISO/IEC 27001:2022 – Informationssicherheitsmanagementsysteme
- OWASP Top 10 (2021) – Web Application Security

## Freigabevermerk

Die Freigabe dieser Dokumentation obliegt der **Geschäftsleitung**
gemeinsam mit der **IT-Leitung** und dem **Informationssicherheits-
beauftragten (ISB)**. Jede produktive Änderung an der Anwendung ist mit
einer entsprechenden Aktualisierung dieser Dokumentation zu
begleiten (vgl. MaRisk AT 5 Tz. 1).

## Copyright

Copyright &copy; 2026 **Volksbank Gronau-Ahaus eG** und **Carsten
Volmer** (Entwicklung). Alle Rechte vorbehalten. Das Lizenzmodell ist
in der Datei [`LICENSE`](../LICENSE) im Projekt-Root dokumentiert.
