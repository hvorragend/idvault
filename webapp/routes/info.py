"""
Info / Hilfe – statische Erläuterungsseiten.

Enthält aktuell ausschließlich das Glossar zur regulatorischen
Begriffsabgrenzung (MaRisk AT 7.2, BAIT, DORA) zwischen
Anwendungsentwicklung, Eigen- und Auftragsprogrammierung, IDV und
Arbeitshilfe.
"""

from flask import Blueprint, render_template
from . import login_required

bp = Blueprint("info", __name__, url_prefix="/hilfe")


# Feste Begriffsabgrenzung nach gängiger Aufsichtspraxis.
# Quelle: Vergleichstabelle aus der internen Regulatorik-Aufstellung.
GLOSSAR = [
    {
        "begriff": "Anwendungsentwicklung",
        "entwickler": "IT-Abt. / Extern",
        "ort": "Zentraler IT-Prozess",
        "fokus": "Gesamter Lebenszyklus (SDLC)",
        "beschreibung": (
            "Oberbegriff für den gesamten Prozess: Anforderung, Konzept, "
            "Programmierung, Test, Rollout und Betrieb. Unterliegt MaRisk AT 7.2 "
            "(Trennprinzip) und DORA (Software-Entwicklungssicherheit)."
        ),
        "im_register": False,
    },
    {
        "begriff": "Eigenprogrammierung",
        "entwickler": "Interne IT",
        "ort": "Zentraler IT-Prozess",
        "fokus": "Code-Qualität, Funktionstrennung",
        "beschreibung": (
            "Das Schreiben des Quellcodes durch internes Personal der IT-Abteilung. "
            "Schutzziele (Vertraulichkeit, Integrität, Verfügbarkeit) müssen je Eigenentwicklung "
            "nachweisbar sein."
        ),
        "im_register": True,
    },
    {
        "begriff": "Auftragsprogrammierung",
        "entwickler": "Externer Dienstleister",
        "ort": "Extern",
        "fokus": "Auslagerungsmanagement, DORA",
        "beschreibung": (
            "Externe Code-Erstellung im Rahmen des IKT-Drittparteien-Risikomanagements. "
            "Verantwortung verbleibt beim Institut – detaillierte Abnahme und "
            "Sicherheitsüberprüfung (Code-Reviews) sind verpflichtend."
        ),
        "im_register": True,
    },
    {
        "begriff": "IDV (Individuelle Datenverarbeitung)",
        "entwickler": "Fachbereich",
        "ort": "Dezentral",
        "fokus": "Schatten-IT vermeiden, Kontrollen",
        "beschreibung": (
            "Durch den Fachbereich entwickelte, wesentliche Anwendungen – z. B. komplexe "
            "Excel-Makros, Access-Datenbanken, SQL-Skripte. Unterliegt dem IDV-Rahmenwerk "
            "nach MaRisk AT 7.2 / BAIT (Dokumentation, Funktionstrennung, Freigabe)."
        ),
        "im_register": True,
    },
    {
        "begriff": "Arbeitshilfe",
        "entwickler": "Fachbereich",
        "ort": "Dezentral (End-User)",
        "fokus": "Wesentlichkeitsprüfung",
        "beschreibung": (
            "Einfache Werkzeuge zur Unterstützung täglicher Aufgaben. Sobald eine "
            "Arbeitshilfe rechnungsrelevant wird, komplexe Logik enthält oder zur "
            "Risikosteuerung dient, wird sie über die Wesentlichkeitsprüfung zur IDV."
        ),
        "im_register": True,
    },
]


@bp.route("/glossar")
@login_required
def glossar():
    return render_template("info/glossar.html", glossar=GLOSSAR)
