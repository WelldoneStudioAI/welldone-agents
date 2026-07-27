"""
core/foncier/sources/seao.py — Appels d'offres publics (SEAO).

Pourquoi le SEAO dans un outil de prospection immobilière : une ville qui lance
un appel d'offres pour un PPU, une requalification de secteur, un prolongement
de ligne ou une réfection majeure annonce publiquement une revalorisation — 12 à
36 mois avant que le marché immobilier l'intègre. C'est le signal le plus en
amont de tout le système, et il est gratuit.

Données Québec publie les avis en JSON hebdomadaire (format inspiré de l'Open
Contracting Data Standard) et l'historique des contrats en XML depuis 2009.

Le parseur est volontairement tolérant : la structure exacte du JSON varie selon
le millésime de publication. Plutôt que de coder un chemin d'accès rigide qui
casse à la prochaine révision du format, on explore récursivement et on retombe
sur une recherche plein texte.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from core.foncier.criteres import (
    SEAO_FENETRE_JOURS,
    SEAO_MOTS_CLES,
    SEAO_ORGANISMES_PERTINENTS,
)
from core.foncier.modele import Immeuble, Signal
from core.foncier.sources._http import (
    SourceIndisponible,
    TTL_HEBDO,
    charger_json,
)

log = logging.getLogger(__name__)

CKAN_BASE = "https://www.donneesquebec.ca/recherche/api/3/action"
JEU_DONNEES = "systeme-electronique-dappel-doffres-seao"

# Clés susceptibles de porter chaque information, tous formats confondus.
CLES_TITRE = ("title", "titre", "tender_title", "objet", "description_courte")
CLES_DESCRIPTION = ("description", "tender_description", "objet_contrat", "details")
CLES_ORGANISME = ("name", "organisme", "procuringentity", "buyer", "donneur", "acheteur")
CLES_DATE = ("date", "datepublication", "publisheddate", "datepublicationavis", "startdate")
CLES_LIEU = ("region", "municipalite", "ville", "lieulivraison", "deliverylocation", "locality")
CLES_URL = ("url", "uri", "lien", "documenturl")


def _normaliser(texte: str) -> str:
    """Minuscules sans accents, pour une comparaison robuste."""
    texte = (texte or "").lower()
    for accentue, simple in (
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("à", "a"), ("â", "a"), ("ô", "o"), ("î", "i"),
        ("ï", "i"), ("û", "u"), ("ù", "u"), ("ç", "c"),
    ):
        texte = texte.replace(accentue, simple)
    return texte


def _chercher_cle(noeud: Any, cles: tuple[str, ...], profondeur: int = 6) -> Optional[str]:
    """
    Cherche récursivement la première valeur textuelle sous l'une des clés.

    Insensible à la casse et aux séparateurs, pour absorber les variations
    `procuringEntity` / `procuring_entity` / `PROCURINGENTITY`.
    """
    if profondeur <= 0:
        return None

    if isinstance(noeud, dict):
        for cle, valeur in noeud.items():
            cle_simple = re.sub(r"[^a-z0-9]", "", str(cle).lower())
            if cle_simple in cles and isinstance(valeur, str) and valeur.strip():
                return valeur.strip()
        for valeur in noeud.values():
            trouve = _chercher_cle(valeur, cles, profondeur - 1)
            if trouve:
                return trouve

    elif isinstance(noeud, list):
        for element in noeud[:20]:
            trouve = _chercher_cle(element, cles, profondeur - 1)
            if trouve:
                return trouve

    return None


def lister_ressources(motif: str = "hebdo") -> list[dict]:
    """
    Interroge l'API CKAN de Données Québec et retourne les ressources du jeu.

    Raises:
        SourceIndisponible: si l'API ne répond pas.
    """
    url = f"{CKAN_BASE}/package_show?id={JEU_DONNEES}"
    try:
        reponse = charger_json(url, ttl=TTL_HEBDO)
    except SourceIndisponible:
        raise
    except (json.JSONDecodeError, ValueError) as e:
        raise SourceIndisponible(f"Réponse CKAN illisible pour {JEU_DONNEES} : {e}") from e

    ressources = (reponse.get("result") or {}).get("resources") or []
    if motif:
        ressources = [r for r in ressources if motif.lower() in (r.get("name") or "").lower()]

    # Les noms suivent hebdo_AAAAMMJJ_AAAAMMJJ : le tri alphabétique est chronologique.
    ressources.sort(key=lambda r: r.get("name") or "", reverse=True)
    log.info("foncier.seao: %d ressource(s) '%s' listée(s)", len(ressources), motif)
    return ressources


def extraire_avis(donnees: Any) -> list[dict]:
    """Aplatit un fichier SEAO en liste d'avis normalisés."""
    # Les avis vivent sous "releases" (OCDS), "avis", ou à la racine.
    lots: Iterable[Any]
    if isinstance(donnees, dict):
        for cle in ("releases", "avis", "records", "data"):
            if isinstance(donnees.get(cle), list):
                lots = donnees[cle]
                break
        else:
            lots = [donnees]
    elif isinstance(donnees, list):
        lots = donnees
    else:
        return []

    avis: list[dict] = []
    for brut in lots:
        if not isinstance(brut, (dict, list)):
            continue
        titre = _chercher_cle(brut, CLES_TITRE) or ""
        description = _chercher_cle(brut, CLES_DESCRIPTION) or ""
        if not titre and not description:
            continue
        avis.append(
            {
                "titre": titre,
                "description": description,
                "organisme": _chercher_cle(brut, CLES_ORGANISME) or "",
                "date": _chercher_cle(brut, CLES_DATE) or "",
                "lieu": _chercher_cle(brut, CLES_LIEU) or "",
                "url": _chercher_cle(brut, CLES_URL) or "",
                # Conservé pour la recherche plein texte de repli.
                "_texte": _normaliser(json.dumps(brut, ensure_ascii=False)[:4000]),
            }
        )

    return avis


def _date_avis(avis: dict) -> Optional[date]:
    texte = (avis.get("date") or "")[:10]
    try:
        return datetime.fromisoformat(texte).date()
    except (ValueError, TypeError):
        return None


def pertinent(avis: dict, fenetre_jours: int = SEAO_FENETRE_JOURS) -> tuple[bool, list[str]]:
    """
    Un avis est retenu s'il émane d'un donneur d'ordre territorial ET qu'il
    touche à l'aménagement ou à l'infrastructure.

    Returns:
        (retenu, mots-clés déclencheurs)
    """
    organisme = _normaliser(avis.get("organisme", ""))
    corpus = _normaliser(f"{avis.get('titre','')} {avis.get('description','')}")
    if not corpus.strip():
        corpus = avis.get("_texte", "")

    territorial = any(o in organisme for o in SEAO_ORGANISMES_PERTINENTS)
    if not territorial:
        # Le donneur d'ordre n'est pas toujours dans le champ attendu : on
        # tolère qu'il apparaisse ailleurs dans l'enregistrement.
        territorial = any(o in avis.get("_texte", "") for o in SEAO_ORGANISMES_PERTINENTS)
    if not territorial:
        return False, []

    declencheurs = [m for m in SEAO_MOTS_CLES if _normaliser(m) in corpus]
    if not declencheurs:
        return False, []

    publie = _date_avis(avis)
    if publie and (date.today() - publie).days > fenetre_jours:
        return False, declencheurs

    return True, declencheurs


def signaux(
    semaines: int = 8, fenetre_jours: int = SEAO_FENETRE_JOURS
) -> list[tuple[str, Signal]]:
    """
    Récupère les avis SEAO récents et retourne les signaux territoriaux.

    Returns:
        Liste de (municipalité normalisée, Signal) — le rattachement aux
        immeubles se fait ensuite par `rattacher()`.
    """
    ressources = lister_ressources("hebdo")[: max(1, semaines)]
    if not ressources:
        log.warning("foncier.seao: aucune ressource hebdomadaire trouvée")
        return []

    resultats: list[tuple[str, Signal]] = []
    vus: set[str] = set()

    for ressource in ressources:
        url = ressource.get("url")
        if not url:
            continue
        try:
            donnees = charger_json(url, ttl=TTL_HEBDO)
        except (SourceIndisponible, json.JSONDecodeError, ValueError) as e:
            log.warning("foncier.seao: %s illisible (%s)", ressource.get("name"), e)
            continue

        for avis in extraire_avis(donnees):
            retenu, declencheurs = pertinent(avis, fenetre_jours)
            if not retenu:
                continue

            empreinte = f"{avis['organisme']}|{avis['titre']}"[:200]
            if empreinte in vus:
                continue
            vus.add(empreinte)

            territoire = _normaliser(avis.get("lieu") or avis.get("organisme") or "")
            # « Ville de Longueuil » → « longueuil »
            territoire = re.sub(
                r"^(ville de|municipalite de|municipalite d|mrc de|mrc d|arrondissement de|arrondissement d)\s+",
                "",
                territoire,
            ).strip()

            # Plus il y a de mots-clés déclenchés, plus le signal est fort.
            intensite = min(1.0, 0.4 + 0.2 * len(declencheurs))

            resultats.append(
                (
                    territoire,
                    Signal(
                        cle="seao_proximite",
                        libelle=(
                            f"Appel d'offres {avis['organisme'] or 'municipal'} : "
                            f"{avis['titre'][:110]}"
                        ),
                        source="seao",
                        date_signal=avis.get("date") or None,
                        url=avis.get("url") or None,
                        intensite=intensite,
                        donnees={"declencheurs": declencheurs, "lieu": avis.get("lieu", "")},
                    ),
                )
            )

    log.info("foncier.seao: %d signal/signaux territorial(aux) retenu(s)", len(resultats))
    return resultats


def rattacher(immeubles: list[Immeuble], signaux_seao: list[tuple[str, Signal]]) -> int:
    """
    Rattache les signaux SEAO aux immeubles par correspondance de territoire.

    Le SEAO ne géolocalise pas ses avis : on rattache donc à la municipalité (ou
    à l'arrondissement) plutôt qu'à un rayon. C'est plus grossier qu'un rayon de
    1,5 km, et c'est assumé — le signal sert à hiérarchiser des secteurs, pas à
    trancher un dossier.

    Returns:
        Nombre de rattachements effectués.
    """
    if not signaux_seao:
        return 0

    par_territoire: dict[str, list[Signal]] = {}
    for territoire, signal in signaux_seao:
        if territoire:
            par_territoire.setdefault(territoire, []).append(signal)

    rattachements = 0
    for immeuble in immeubles:
        cles = [
            _normaliser(immeuble.municipalite or ""),
            _normaliser(immeuble.arrondissement or ""),
        ]
        for cle in cles:
            if not cle:
                continue
            for territoire, liste in par_territoire.items():
                if not territoire:
                    continue
                if territoire in cle or cle in territoire:
                    # On ne retient que le signal le plus fort par immeuble.
                    meilleur = max(liste, key=lambda s: s.intensite)
                    immeuble.ajouter_signal(meilleur)
                    rattachements += 1
                    break
            else:
                continue
            break

    log.info("foncier.seao: %d immeuble(s) rattaché(s) à un signal SEAO", rattachements)
    return rattachements


def horizon_seao(jours: int = 30) -> str:
    """Résumé lisible des avis pertinents des N derniers jours, pour un brief."""
    recents = signaux(semaines=max(1, jours // 7), fenetre_jours=jours)
    if not recents:
        return "Aucun avis SEAO pertinent sur la période."

    lignes = [f"{len(recents)} avis SEAO pertinent(s) sur {jours} jours :"]
    for territoire, signal in recents[:15]:
        lignes.append(f"  · [{territoire or 'territoire ?'}] {signal.libelle}")
    return "\n".join(lignes)


def borne_periode(semaines: int) -> tuple[date, date]:
    """Bornes de la période couverte, pour l'affichage."""
    fin = date.today()
    return fin - timedelta(weeks=semaines), fin
