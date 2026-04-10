# idvault – Data Model

> Discover, classify and govern individual data processing (IDV) assets across your organization — built for MaRisk AT 7.2 and DORA compliance.

This document describes the relational database schema that forms the backbone of **idvault**. The model is implemented in SQLite and covers the full lifecycle of an IDV asset — from automated discovery on network shares to classification, approval, periodic review, and eventual decommissioning.

---

## Table of Contents

- [Overview](#overview)
- [Regulatory Background](#regulatory-background)
- [Schema Diagram](#schema-diagram)
- [Tables](#tables)
  - [Reference / Lookup Tables](#reference--lookup-tables)
  - [Core Register](#core-register)
  - [Workflow & Audit](#workflow--audit)
- [Views](#views)
- [Classification Fields](#classification-fields)
  - [IDV Type](#idv-type)
  - [GDA – Degree of Dependency](#gda--degree-of-dependency)
  - [Workflow Status](#workflow-status)
- [Getting Started](#getting-started)
- [Design Decisions](#design-decisions)

---

## Overview

The data model is split into three logical layers:

| Layer | Purpose |
|---|---|
| **Reference data** | Org units, persons, business processes, platforms, risk classes |
| **Core register** | The `idv_register` table — one row per IDV asset, ~70 attributes |
| **Workflow & audit** | History, reviews, measures, approvals, document references |

The scanner component (`idv_scanner.py`) writes raw file metadata into `idv_files`. The register layer (`idv_register`) links to those scan results and adds the curated classification on top.

Both tables can live in the same SQLite database file or in separate files — the `idv_files` table is included in `schema.sql` as a stub to maintain referential integrity when used together.

---

## Regulatory Background

| Regulation | Relevant Provision | Addressed By |
|---|---|---|
| MaRisk | AT 7.2 — IDV governance requirements | Full register lifecycle |
| BAIT | Tz. 52–56 — IDV risk classification | `gda_wert`, risk dimensions |
| DORA | Art. 28 / 30 — critical/important functions | `dora_kritisch_wichtig` flag |
| GDPR / BDSG | Personal data processing | `enthaelt_personendaten`, `datenschutz_kategorie` |

---

## Schema Diagram

```
org_units ──────────────────────────────────────────────┐
persons ─────────────────────────────────────────────┐  │
geschaeftsprozesse ───────────────────────────────┐  │  │
plattformen ──────────────────────────────────┐   │  │  │
risikoklassen ────────────────────────────┐   │   │  │  │
                                          │   │   │  │  │
idv_files ──────────────────────────────► idv_register ◄┘
                                               │
                 ┌─────────────────────────────┼──────────────────────┐
                 │                             │                      │
            idv_history                    pruefungen           massnahmen
                                               │
                                          genehmigungen
                                          idv_abhaengigkeiten
                                          dokumente
```

---

## Tables

### Reference / Lookup Tables

#### `org_units` — Organisational Units

Hierarchical structure of departments and teams. Used to assign ownership and filter the register by area of the bank.

| Column | Type | Description |
|---|---|---|
| `kuerzel` | TEXT | Short code, e.g. `KRE`, `BWK` |
| `bezeichnung` | TEXT | Full name |
| `ebene` | TEXT | `Vorstand` / `Bereich` / `Abteilung` |
| `parent_id` | INTEGER | Self-referential FK for hierarchy |

#### `persons` — People

All individuals involved in the IDV lifecycle: developers, business owners, reviewers, approvers.

| Column | Type | Description |
|---|---|---|
| `kuerzel` | TEXT | Unique short code, e.g. `MMA` |
| `rolle` | TEXT | `IDV-Koordinator`, `Fachverantwortlicher`, `Revision` … |
| `org_unit_id` | FK | Assigned organisational unit |

#### `geschaeftsprozesse` — Business Processes

The bank's process catalogue. IDV assets are mapped to one process entry, enabling criticality inheritance.

| Column | Type | Description |
|---|---|---|
| `gp_nummer` | TEXT | Unique process ID, e.g. `GP-KRE-001` |
| `ist_kritisch` | INTEGER | Critical/important per DORA Art. 28 |
| `ist_wesentlich` | INTEGER | Material per MaRisk |

#### `plattformen` — Technology Platforms

Host systems on which IDV assets run: `Microsoft Excel 2021`, `Python 3.11`, `Power BI Desktop`, etc.

#### `risikoklassen` — Risk Classes

Pre-seeded with four levels: **Kritisch · Hoch · Mittel · Gering**. Colours and sort order are configurable.

---

### Core Register

#### `idv_register` — The IDV Register

The central table. One row per IDV asset. Key field groups:

**Identification**

| Column | Description |
|---|---|
| `idv_id` | Human-readable ID: `IDV-2025-001` (auto-generated per year) |
| `bezeichnung` | Descriptive name |
| `version` | Version string, e.g. `2.1` |
| `file_id` | Link to scanner result in `idv_files` |

**Classification**

| Column | Description |
|---|---|
| `idv_typ` | Asset type — see [IDV Type](#idv-type) |
| `steuerungsrelevant` | Management-relevant (MaRisk AT 7.2) — requires written justification |
| `relevant_guv` | Relevant to the P&L statement |
| `relevant_meldewesen` | Relevant to regulatory reporting |
| `relevant_risikomanagement` | Relevant to risk management |
| `rechnungslegungsrelevant` | Accounting-relevant — requires written justification |
| `gda_wert` | Degree of dependency 1–4 — see [GDA](#gda--degree-of-dependency) |
| `dora_kritisch_wichtig` | Critical/important function per DORA Art. 28 |

**Risk Assessment**

| Column | Description |
|---|---|
| `risikoklasse_id` | Overall risk class (FK → `risikoklassen`) |
| `risiko_verfuegbarkeit` | Availability risk 1–5 |
| `risiko_integritaet` | Integrity risk 1–5 |
| `risiko_vertraulichkeit` | Confidentiality risk 1–5 |
| `risiko_nachvollziehbarkeit` | Auditability risk 1–5 |

**Ownership**

| Column | Description |
|---|---|
| `org_unit_id` | Responsible department |
| `fachverantwortlicher_id` | Business owner |
| `idv_entwickler_id` | Developer / creator |
| `idv_koordinator_id` | IDV coordinator for the department |
| `stellvertreter_id` | Deputy |

**Quality & Controls**

| Column | Description |
|---|---|
| `dokumentation_vorhanden` | Documentation exists |
| `testkonzept_vorhanden` | Test concept exists |
| `versionskontrolle` | Version control in use (Git etc.) |
| `zugriffsschutz` | Access protection in place |
| `vier_augen_prinzip` | Four-eyes principle applied |

**Lifecycle**

| Column | Description |
|---|---|
| `pruefintervall_monate` | Review interval in months (default: 12) |
| `naechste_pruefung` | Date of next scheduled review |
| `abloesung_geplant` | Decommissioning planned |
| `abloesung_zieldatum` | Target decommissioning date |
| `status` | Workflow status — see [Workflow Status](#workflow-status) |

---

### Workflow & Audit

#### `idv_history` — Change History

Every change to an `idv_register` row is recorded here as a JSON delta:

```json
{
  "gda_wert": { "alt": 3, "neu": 4 },
  "status":   { "alt": "Entwurf", "neu": "In Prüfung" }
}
```

Actions: `erstellt` · `geaendert` · `status_geaendert` · `geprueft` · `kommentar`

#### `pruefungen` — Reviews

Each periodic or ad-hoc review is stored as a separate row linked to the IDV. Outcomes: `Ohne Befund` · `Mit Befund` · `Kritischer Befund` · `Nicht bestanden`.

#### `massnahmen` — Remediation Measures

Measures arising from reviews or identified proactively. Tracked with priority (`Kritisch` → `Niedrig`), responsible person, due date, and completion status.

#### `genehmigungen` — Approvals

Two-stage approval workflow:

| Stage | Approver | Required when |
|---|---|---|
| Stage 1 | IDV coordinator / business owner | Always |
| Stage 2 | IT Security / Internal Audit | GDA = 4 or DORA-critical |

#### `idv_abhaengigkeiten` — Dependencies

Directed graph of IDV-to-IDV relationships. Relation types: `Datenlieferant` · `Datenempfänger` · `Steuert` · `Wird gesteuert von`.

#### `dokumente` — Document References

Links to supporting documents stored on SharePoint or network shares (fachkonzept, test protocol, approval record, risk analysis).

---

## Views

Five pre-built views for common reporting needs:

| View | Description |
|---|---|
| `v_idv_uebersicht` | Full overview of all active IDV assets with computed review status |
| `v_kritische_idvs` | All assets where GDA=4 OR management-relevant OR DORA-critical |
| `v_offene_massnahmen` | Open measures with escalation flag (overdue / due soon) |
| `v_unvollstaendige_idvs` | Quality gate: assets missing mandatory fields |
| `v_prueffaelligkeiten` | Reviews due within the next 90 days |

---

## Classification Fields

### IDV Type

| Value | Description |
|---|---|
| `Excel-Tabelle` | Data table, no macros |
| `Excel-Makro` | Excel with VBA macros (XLSM/XLSB) |
| `Excel-Modell` | Complex calculation model |
| `Access-Datenbank` | MDB / ACCDB |
| `Python-Skript` | Python-based automation |
| `SQL-Skript` | Direct database queries |
| `Power-BI-Bericht` | PBIX with data transformation logic |
| `Sonstige` | Other |
| `unklassifiziert` | Not yet assessed (initial state) |

### GDA – Degree of Dependency

Derived from BAIT guidance on IDV risk classification:

| Value | Label | Meaning |
|---|---|---|
| 1 | Unterstützend | Process runs without IDV, with additional manual effort |
| 2 | Relevant | IDV supports process; alternative process exists |
| 3 | Wesentlich | Core process support; no complete manual alternative |
| 4 | Vollständig abhängig | Process cannot be executed without this IDV |

GDA = 4 triggers the second approval stage and mandatory DORA assessment.

### Workflow Status

```
Entwurf
  │
  ▼
In Prüfung ──► Abgelehnt
  │
  ▼
Genehmigt ◄── Genehmigt mit Auflagen
  │
  ▼
Abgekündigt
  │
  ▼
Archiviert
```

---

## Getting Started

```bash
# Install dependencies
pip install -r requirements.txt

# Initialise the database
python db.py --db idvault.db

# Load demo data (optional)
python db.py --db idvault.db --demo

# Run the network scanner
python idv_scanner.py --config config.json

# Export to Excel
python idv_export.py --db idvault.db
```

---

## Design Decisions

**SQLite as the primary store** — SQLite in WAL mode handles concurrent reads from multiple users well and requires zero infrastructure. Migration to PostgreSQL is straightforward if the user base grows beyond ~50 simultaneous writers.

**ISO 8601 strings for all dates** — avoids timezone handling issues across different Python and OS versions. All timestamps are stored in UTC.

**JSON fields for structured lists** — `tags`, `schnittstellen`, `weitere_dateien` and the history delta are stored as JSON strings. This avoids unnecessary join tables for data that is always read as a whole and never filtered by individual elements.

**Separation of scanner and register** — `idv_files` holds raw, automated discovery data. `idv_register` holds the curated, human-validated classification. Both can live in the same database file. This separation means the scanner can run unattended and overwrite scan results without touching the register.

**Justification fields are mandatory by convention, not by constraint** — fields like `steuerungsrelevanz_begr` are enforced at the application layer (and flagged by `v_unvollstaendige_idvs`) rather than as `NOT NULL` constraints, to allow saving incomplete drafts during the classification workflow.
