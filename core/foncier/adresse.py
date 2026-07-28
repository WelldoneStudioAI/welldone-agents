"""
core/foncier/adresse.py — Normalisation d'adresses québécoises.

Le problème que ce module règle : le même immeuble s'écrit différemment selon la
source. Le rôle d'évaluation dit « 1450 ONTARIO E », Centris dit « 1450, rue
Ontario Est », un avis de MRC dit « 1450 rue Ontario est, Montréal ». Sans
normalisation, ce sont trois immeubles distincts et le croisement ne marche pas.

La clé produite est volontairement grossière — numéro civique + rue réduite à
l'essentiel + municipalité. Elle privilégie les faux positifs aux faux négatifs :
mieux vaut rapprocher deux fiches à vérifier que rater le rapprochement qui vaut
de l'argent.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Vocabulaire
# ─────────────────────────────────────────────────────────────────────────────

# Types de voie : toutes les graphies ramenées à une forme unique.
TYPES_VOIE = {
    "rue": "rue", "r": "rue",
    "avenue": "av", "av": "av", "ave": "av",
    "boulevard": "boul", "boul": "boul", "bd": "boul", "blvd": "boul",
    "chemin": "ch", "ch": "ch",
    "route": "rte", "rte": "rte", "rt": "rte",
    "rang": "rang",
    "place": "pl", "pl": "pl",
    "montee": "montee",
    "cote": "cote",
    "terrasse": "tsse", "tsse": "tsse", "terr": "tsse",
    "impasse": "imp", "imp": "imp",
    "croissant": "crois", "crois": "crois",
    "allee": "allee",
    "carre": "carre",
    "promenade": "prom", "prom": "prom",
    "autoroute": "aut", "aut": "aut",
    "voie": "voie",
    "cercle": "cercle",
    "square": "square",
}

# Points cardinaux : « E », « Est », « est » → « est ». Jamais supprimés :
# Ontario Est et Ontario Ouest sont deux rues différentes.
CARDINAUX = {
    "e": "est", "est": "est",
    "o": "ouest", "ouest": "ouest", "w": "ouest", "west": "ouest",
    "n": "nord", "nord": "nord", "north": "nord",
    "s": "sud", "sud": "sud", "south": "sud",
}

# Mots vides à retirer du nom de rue : ils varient d'une source à l'autre.
MOTS_VIDES = {"de", "du", "des", "d", "la", "le", "les", "l", "a", "au", "aux"}

# Indicateurs d'appartement — tout ce qui suit est ignoré.
MOTIF_APPARTEMENT = re.compile(
    r"[,\s]+(?:app\.?|apt\.?|appartement|suite|bureau|unite|unit|local|#)\s*[\w-]*.*$",
    re.IGNORECASE,
)

MOTIF_CODE_POSTAL = re.compile(r"\b[a-z]\d[a-z]\s*\d[a-z]\d\b", re.IGNORECASE)


def sans_accents(texte: str) -> str:
    """Retire les accents et met en minuscules."""
    if not texte:
        return ""
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn").lower()


def normaliser_municipalite(municipalite: str) -> str:
    """
    Réduit une municipalité à sa forme canonique.

    « Montréal (Rosemont—La Petite-Patrie) » → « montreal »
    « Ville de Longueuil »                   → « longueuil »
    « St-Jean-sur-Richelieu »                → « stjeansurrichelieu »
    """
    texte = sans_accents(municipalite)

    # L'arrondissement entre parenthèses n'est pas la municipalité.
    texte = re.sub(r"\(.*?\)", " ", texte)

    texte = re.sub(
        r"^\s*(?:ville de|ville d|municipalite de|municipalite d|mrc de|mrc d|"
        r"paroisse de|canton de|village de)\s+",
        "",
        texte,
    )

    # Saint / Sainte sous toutes leurs formes.
    texte = re.sub(r"\bsainte\b|\bste\b", "ste", texte)
    texte = re.sub(r"\bsaint\b|\bst\b", "st", texte)

    return re.sub(r"[^a-z0-9]", "", texte)


def _numero_civique(texte: str) -> tuple[Optional[str], str]:
    """
    Sépare le numéro civique du reste.

    « 1450-1454 rue Ontario » → ("1450", "rue Ontario")
    Sur une plage, on retient le premier numéro : c'est celui que toutes les
    sources publient.
    """
    correspondance = re.match(r"^\s*(\d+)\s*(?:[-–/]\s*\d+)?\s*[,\s]\s*(.*)$", texte)
    if correspondance:
        return correspondance.group(1), correspondance.group(2)

    # Numéro seul, sans rue.
    seul = re.match(r"^\s*(\d+)\s*$", texte)
    if seul:
        return seul.group(1), ""

    return None, texte.strip()


def normaliser_rue(rue: str) -> str:
    """
    Réduit un nom de rue à ses composantes signifiantes.

    « rue Ontario Est »        → « rue|ontario|est »
    « Ontario E. »             → « ontario|est »
    « boulevard de Maisonneuve » → « boul|maisonneuve »
    """
    texte = sans_accents(rue)
    texte = MOTIF_CODE_POSTAL.sub(" ", texte)
    texte = re.sub(r"[^\w\s-]", " ", texte)
    texte = texte.replace("-", " ")

    jetons = [j for j in texte.split() if j]
    resultat: list[str] = []

    for jeton in jetons:
        if jeton in MOTS_VIDES:
            continue
        if jeton in TYPES_VOIE:
            resultat.append(TYPES_VOIE[jeton])
            continue
        if jeton in CARDINAUX:
            resultat.append(CARDINAUX[jeton])
            continue
        # Saint / Sainte harmonisés.
        if jeton in ("saint", "st"):
            resultat.append("st")
            continue
        if jeton in ("sainte", "ste"):
            resultat.append("ste")
            continue
        resultat.append(jeton)

    return "|".join(resultat)


# Types de voie : présents ou absents selon la source (« rue Ontario » vs
# « Ontario »). Ils sont donc retirés de la clé — sinon le même immeuble
# produirait deux clés différentes.
JETONS_TYPE_VOIE = set(TYPES_VOIE.values())


def cle(adresse: str, municipalite: str = "") -> str:
    """
    Clé de rapprochement d'un immeuble entre deux sources.

    Format : « municipalite:civique:rue ». Le type de voie est écarté parce
    qu'il est facultatif d'une source à l'autre ; les points cardinaux sont
    conservés parce qu'ils distinguent de vraies rues différentes.

        cle("1450 rue Ontario Est", "Montréal") → "montreal:1450:ontario|est"
        cle("1450, Ontario E.", "Montreal")     → "montreal:1450:ontario|est"
        cle("1450 rue Ontario Ouest", "Mtl")    → "…:1450:ontario|ouest"  (différent)
    """
    adresse = MOTIF_APPARTEMENT.sub("", adresse or "")
    civique, rue = _numero_civique(adresse)
    jetons = [j for j in normaliser_rue(rue).split("|") if j and j not in JETONS_TYPE_VOIE]
    return f"{normaliser_municipalite(municipalite)}:{civique or ''}:{'|'.join(jetons)}"


def composantes(adresse: str, municipalite: str = "") -> tuple[str, str, set[str]]:
    """
    Décompose une adresse en (municipalité, numéro civique, jetons de rue).

    Les jetons servent au rapprochement souple : deux adresses correspondent si
    leurs jetons signifiants se recouvrent suffisamment.
    """
    adresse = MOTIF_APPARTEMENT.sub("", adresse or "")
    civique, rue = _numero_civique(adresse)
    jetons = set(normaliser_rue(rue).split("|")) - {""}
    return normaliser_municipalite(municipalite), civique or "", jetons


# Jetons incapables de porter à eux seuls la preuve qu'il s'agit de la même rue.
# Les types de voie (toutes les rues en ont un) et les points cardinaux
# (« Ontario Est » et « Sherbrooke Est » partagent « est » sans rien d'autre).
JETONS_FAIBLES = set(TYPES_VOIE.values()) | set(CARDINAUX.values())

# Longueur minimale d'un préfixe de municipalité pour être considéré probant.
PREFIXE_MUNICIPALITE_MIN = 5


def memes_municipalites(municipalite_a: str, municipalite_b: str) -> bool:
    """
    Deux libellés désignent-ils la même municipalité ?

    Tolère le suffixe d'arrondissement, très fréquent : une URL Centris donne
    « montreal-rosemont-la-petite-patrie » là où le rôle dit « Montréal ». On
    accepte donc qu'un libellé soit le préfixe de l'autre.
    """
    a = normaliser_municipalite(municipalite_a)
    b = normaliser_municipalite(municipalite_b)

    if not a or not b:
        return True  # information absente : on ne tranche pas là-dessus
    if a == b:
        return True

    court, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(court) >= PREFIXE_MUNICIPALITE_MIN and long.startswith(court)


def correspond(
    adresse_a: str,
    municipalite_a: str,
    adresse_b: str,
    municipalite_b: str,
    exiger_meme_municipalite: bool = True,
) -> bool:
    """
    Deux adresses désignent-elles le même immeuble ?

    Règle : même municipalité, même numéro civique, et au moins un jeton de rue
    signifiant en commun — « ontario » compte, « rue » et « est » ne comptent pas.

    C'est délibérément permissif sur la municipalité et strict sur le nom de
    rue. Un faux rapprochement se voit immédiatement dans le brief ; un
    rapprochement manqué te fait passer à côté du fait que l'immeuble suivi
    depuis huit mois vient d'arriver sur le marché.
    """
    mun_a, civ_a, jetons_a = composantes(adresse_a, municipalite_a)
    mun_b, civ_b, jetons_b = composantes(adresse_b, municipalite_b)

    if exiger_meme_municipalite and not memes_municipalites(mun_a, mun_b):
        return False

    # Le numéro civique est le discriminant fort : sans lui, on ne rapproche pas.
    if not civ_a or not civ_b or civ_a != civ_b:
        return False

    communs = (jetons_a & jetons_b) - JETONS_FAIBLES
    return bool(communs)
