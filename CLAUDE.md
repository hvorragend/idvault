# Projekt-Notizen für Claude Code

## Commit-Konvention: Eindeutige Issue-Verweise

Jeder Commit, der ein GitHub-Issue umsetzt, MUSS einen eindeutigen Verweis
auf das Ticket enthalten. „Eindeutig" heißt:

1. **Pro Ticket ein eigener Commit.** Mehrere Tickets in einem Commit
   nicht zusammenfassen, auch wenn sie thematisch verwandt sind. So
   landet jeder Commit auf der jeweiligen Issue-Seite und ist
   nachvollziehbar.

2. **GitHub-Closing-Keyword im Commit-Body.** Eine Zeile mit
   `Closes #<nummer>` (oder `Fixes #<nummer>` bei Bugs) im Body. GitHub
   verlinkt den Commit damit automatisch im Issue und schließt es beim
   Merge in den Default-Branch.

3. **Issue-Nummer im Titel.** Der Commit-Titel beginnt mit der
   Issue-Nummer in eckigen Klammern oder als Suffix, z. B.:
   - `[#399] Billion-Laughs-DoS via defusedxml schließen` oder
   - `Billion-Laughs-DoS via defusedxml schließen (#399)`

Beispiel:

```
[#399] Billion-Laughs-DoS via defusedxml schließen

scanner/network_scanner.py importiert jetzt defusedxml.ElementTree als
Drop-in-Replacement; Stdlib-Fallback nur für minimale Build-Umgebungen.
defusedxml>=0.7.1 in requirements.txt.

Closes #399
```

**Hintergrund:** GitHub zeigt einen Commit auf der Issue-Seite nur dann,
wenn (a) der Branch gemerged wurde, (b) ein PR offen ist, der das Issue
nennt, oder (c) der Commit ein Closing-Keyword enthält. Ohne das
Keyword ist „der Commit existiert auf einem Branch" praktisch
unsichtbar – das war der Auslöser für diese Regel.

## Keine personenbezogenen Daten in Commits oder Code-Kommentaren

Commit-Nachrichten, Code-Kommentare und Doc-Strings dürfen KEINE
personenbezogenen Daten enthalten. Das umfasst – auch wenn sie aus
einem Bug-Report, Screenshot oder Log stammen:

- Vor- oder Nachnamen realer Personen (Mitarbeiter, Kunden, Dritte)
- Konkrete Datei- oder Ordnernamen aus Produktivlaufwerken,
  insbesondere wenn sie Personennamen, Mandanten oder Vorgangsdaten
  enthalten
- Benutzerkennungen, E-Mail-Adressen, Telefonnummern, IP-Adressen,
  UNC-Pfade, die auf konkrete Shares zeigen

Stattdessen abstrahieren: „Datei mit Umlaut im Namen", „interner
Share", „Mitarbeiter-Account". Wenn ein konkretes Beispiel zur
Reproduktion nötig ist, generische Platzhalter („Müller, Max",
„Beispieldatei.xlsx") verwenden, die eindeutig als Beispiel erkennbar
sind. Diese Regel gilt auch für PR-Beschreibungen, Issue-Kommentare
und ähnliches.
