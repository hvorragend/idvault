# idvault

Erfassung, Klassifizierung und Überwachung von Individuellen Datenverarbeitungen (IDV)
nach **MaRisk AT 7.2** und **DORA** — gebaut für Volksbanken und Sparkassen.

---

## Schnellstart

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:5000
# → Login: admin / idvault2025
```

---

## Funktionsübersicht

### Dashboard

Einstiegsseite mit Kennzahlen auf einen Blick:
- Anzahl aktiver IDVs nach Status (Entwurf / In Prüfung / Genehmigt)
- Kritische IDVs (GDA 4, steuerungsrelevant, DORA-kritisch)
- Überfällige und bald fällige Prüfungen
- Offene Maßnahmen mit Eskalationsstatus

---

### IDV-Grundgesamtheit

Liste aller registrierten IDVs. Filterbar nach Status, GDA-Wert, Typ und
Compliance-Profil (DORA-kritisch, steuerungsrelevant, unvollständig).

**Neue IDV erfassen:** Über *„Neue IDV"* oder direkt aus einem Scannerfund heraus
(siehe Scanner-Funde). Das Formular führt durch fünf Abschnitte:

1. **Stammdaten** — Bezeichnung, Typ, Version, Kurzbeschreibung
2. **Klassifizierung** — GDA-Wert (1–4), Steuerungsrelevanz, RL-Relevanz, DORA
3. **Risikobewertung** — Risikoklasse, Verfügbarkeit, Integrität, Vertraulichkeit
4. **Technik & Betrieb** — Plattform, Nutzungsfrequenz, Zugriffsschutz, Makros
5. **Verantwortliche** — Org-Einheit, Fachverantwortlicher, Entwickler, Koordinator

---

### Prüfungen

**Wozu:** MaRisk AT 7.2 schreibt vor, dass IDVs in regelmäßigen Abständen geprüft
werden. Das Prüfintervall (z.B. 6 oder 12 Monate) wird pro IDV festgelegt.
Die Prüfungen-Ansicht zeigt alle Prüfungen IDV-übergreifend — nützlich um
z.B. alle überfälligen Prüfungen auf einen Blick zu sehen.

**Wie eine Prüfung angelegt wird:**

```
IDV-Grundgesamtheit → IDV auswählen → Detailseite → „Neue Prüfung"
```

Eine Prüfung dokumentiert:

| Feld | Beschreibung |
|---|---|
| Prüfungsart | Regelprüfung / Anlassprüfung / Erstprüfung |
| Prüfungsdatum | Datum der Durchführung |
| Prüfer | Person aus dem Personenkatalog |
| Ergebnis | Ohne Befund / Mit Befund / Kritischer Befund / Nicht bestanden |
| Befundbeschreibung | Freitext zu festgestellten Mängeln |
| Nächste Prüfung | Datum → wird automatisch ins IDV-Register übernommen |
| Kommentar | Interne Anmerkungen |

Nach dem Speichern wird `naechste_pruefung` im IDV-Register aktualisiert und der
Prüfstatus im Dashboard und in den Übersichtslisten neu berechnet.

**Filter in der Listenansicht:**
- *Standard:* alle Prüfungen der letzten 100 Einträge
- *Überfällig:* IDVs, deren `naechste_pruefung` in der Vergangenheit liegt

---

### Maßnahmen

**Wozu:** Wenn eine Prüfung Mängel ergibt oder Risiken proaktiv erkannt werden,
entstehen daraus Maßnahmen. Die Maßnahmen-Ansicht zeigt alle offenen Maßnahmen
IDV-übergreifend — nützlich für den IDV-Koordinator als Gesamtüberblick.

**Wie eine Maßnahme angelegt wird:**

```
IDV-Grundgesamtheit → IDV auswählen → Detailseite → „Neue Maßnahme"
```

Eine Maßnahme enthält:

| Feld | Beschreibung |
|---|---|
| Titel | Kurze Beschreibung der Maßnahme |
| Beschreibung | Ausführliche Erläuterung |
| Maßnahmentyp | z.B. Dokumentation / Zugriffsschutz / Ablösung |
| Priorität | Kritisch / Hoch / Mittel / Niedrig |
| Verantwortlicher | Person aus dem Personenkatalog |
| Fällig am | Zieldatum für die Erledigung |
| Status | Offen → In Bearbeitung → Erledigt |

**Status-Workflow:**

```
Offen → In Bearbeitung → Erledigt
```

Über den Button *„Als erledigt markieren"* auf der IDV-Detailseite wird
eine Maßnahme mit Erledigungsdatum abgeschlossen.

**Filter in der Listenansicht:**
- *Standard:* alle offenen und in Bearbeitung befindlichen Maßnahmen
- *Überfällig:* Maßnahmen, deren Fälligkeitsdatum überschritten ist

---

### Scanner-Funde

Zeigt alle Dateien, die der IDV-Scanner auf Netzlaufwerken gefunden hat.
Über den Button *„Als IDV registrieren"* wird das IDV-Formular mit vorausgefüllten
Daten (Dateiname, IDV-Typ aus Erweiterung, Makro-Flag) geöffnet.

**Filter:**
- Alle aktiven Dateien
- Noch nicht registriert (kein IDV-Eintrag verknüpft)
- Mit Makros (VBA)
- Bereits registriert
- **Archiv** — Dateien, die beim letzten Scan nicht mehr gefunden wurden
  (verschoben, umbenannt oder gelöscht). Die Verknüpfung zum IDV-Register
  bleibt erhalten. Taucht eine Datei wieder auf, wird sie automatisch reaktiviert.

Voraussetzung: Scanner und Webapp müssen dieselbe Datenbank nutzen.
Dazu in `scanner/config.json` setzen:
```json
{ "db_path": "../instance/idvault.db" }
```

→ Weitere Details: [`scanner/README.md`](scanner/README.md)

---

### Administration

Verwaltung der Stammdaten:
- **Personen** — Fachverantwortliche, Entwickler, Koordinatoren, Prüfer
- **Org-Einheiten** — Abteilungen und Bereiche
- **Geschäftsprozesse** — Prozesskatalog (Basis für Kritikalitätsbewertung)
- **Plattformen** — Technologie-Katalog (Excel, Python, Power BI …)
- **Risikoklassen** — Kritisch / Hoch / Mittel / Gering

---

## Typischer Workflow

```
1. Scanner läuft (wöchentlich per Scheduled Task)
        ↓
2. Scanner-Funde → „Als IDV registrieren"
        ↓
3. IDV-Formular ausfüllen (GDA, Klassifizierung, Verantwortliche)
        ↓
4. Status: Entwurf → In Prüfung → Genehmigt
        ↓
5. Regelprüfung fällig (nach pruefintervall_monate)
        ↓
6. Prüfung dokumentieren → Ergebnis + nächstes Prüfdatum
        ↓
7. Bei Befund: Maßnahme anlegen → verfolgen bis Erledigt
        ↓
8. Dashboard zeigt Gesamtstatus jederzeit aktuell
```

---

## Komponenten

| Verzeichnis | Inhalt |
|---|---|
| `webapp/` | Flask-Webanwendung (Blueprints, Templates, DB-Schicht) |
| `scanner/` | IDV-Scanner für Netzlaufwerke |
| `schema.sql` | SQLite-Schema (IDV-Register, Workflow-Tabellen) |
| `db.py` | Datenbankschicht (gemeinsam von Scanner und Webapp genutzt) |
| `run.py` | Startskript für die Webapp |
| `data_model/` | Datenmodell-Dokumentation |

→ Technisches Datenmodell: [`data_model/README.md`](data_model/README.md)
