"""
core/foncier/scoring.py — Notation déterministe des candidats.

Volontairement en Python, pas en LLM. Trois raisons :
  1. Reproductible — le même immeuble donne toujours le même score, ce qui rend
     le classement défendable et permet de backtester les poids sur les
     millésimes 2008-2025 du rôle.
  2. Gratuit — on score des centaines de milliers d'unités par passe.
  3. Auditable — `detail_score` explique chaque point attribué.

Claude n'intervient qu'après, sur les 10 meilleurs dossiers, pour rédiger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from core.foncier.criteres import (
    CUBF_TERRAIN_VAGUE,
    Criteres,
    DEFAUT,
    GROUPES_DEVELOPPEMENT_PUR,
    POIDS,
    POIDS_PAR_CLE,
    SCORE_MAX,
    SIGNAUX_INFORMATIFS,
    groupe_cubf,
)
from core.foncier.modele import Immeuble

log = logging.getLogger(__name__)


@dataclass
class Rejet:
    """Motif d'exclusion d'un immeuble, pour que rien ne disparaisse en silence."""

    identifiant: str
    adresse: str
    motif: str


# ─────────────────────────────────────────────────────────────────────────────
# Filtres éliminatoires
# ─────────────────────────────────────────────────────────────────────────────


def eligible(immeuble: Immeuble, criteres: Criteres = DEFAUT) -> tuple[bool, str]:
    """
    Applique les filtres éliminatoires de la thèse.

    Returns:
        (True, "") si l'immeuble mérite d'être scoré, sinon (False, motif).
    """
    groupe = groupe_cubf(immeuble.cubf)

    # Municipalité hors périmètre.
    if criteres.municipalites:
        cibles = {m.strip().lower() for m in criteres.municipalites}
        if (immeuble.municipalite or "").strip().lower() not in cibles:
            return False, f"hors périmètre ({immeuble.municipalite})"

    # Terrain vague / immeuble non exploité : filière développement pur, exempté
    # du filtre d'usage et de la bande de prix basse (un terrain à 400 000 $ dans
    # une aire TOD reste une occasion de développement).
    if groupe in GROUPES_DEVELOPPEMENT_PUR:
        if not criteres.inclure_terrains:
            return False, "terrains exclus par les critères"
        if immeuble.valeur_totale and immeuble.valeur_totale > criteres.capture_max:
            return False, f"valeur {immeuble.valeur_totale:,.0f} $ au-dessus de la bande"
        return True, ""

    # Usage.
    if groupe is None:
        return False, "code d'utilisation absent ou illisible"
    if groupe not in criteres.groupes_cibles:
        return False, f"usage hors thèse (groupe CUBF {groupe})"

    # Un immeuble résidentiel doit être multirésidentiel pour compter.
    from core.foncier.criteres import GROUPE_RESIDENTIEL

    if groupe == GROUPE_RESIDENTIEL:
        logements = immeuble.nombre_logements or 0
        if logements < criteres.logements_min:
            return False, f"{logements} logement(s), minimum {criteres.logements_min}"

    # Bande de prix, élargie par la tolérance rôle/marché.
    valeur = immeuble.valeur_totale
    if valeur is None:
        return False, "aucune valeur au rôle"
    if valeur < criteres.capture_min:
        return False, f"valeur {valeur:,.0f} $ sous la bande"
    if valeur > criteres.capture_max:
        return False, f"valeur {valeur:,.0f} $ au-dessus de la bande"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Sous-utilisation du terrain
# ─────────────────────────────────────────────────────────────────────────────

# Ratio bâti/terrain au-dessus duquel un terrain est considéré comme construit à
# sa mesure, par groupe CUBF. En deçà, l'écart est traité comme du constructible
# non construit.
#
# Ordres de grandeur observés : un commerce de plain-pied avec stationnement
# affiche 0,15-0,30 ; un multiplex urbain, 0,45-0,70 ; un immeuble de coin bâti
# sur toute son emprise, plus de 0,80.
#
# ⚠️ Le champ « superficie bâtiment » du rôle désigne l'emprise au sol dans
#    certaines municipalités et la superficie totale des étages dans d'autres.
#    Le ratio n'est donc comparable qu'à l'intérieur d'une même source. Vérifier
#    sur quelques immeubles connus avant de se fier au signal sur un nouveau
#    territoire.
SEUIL_SOUS_UTILISATION = {
    1: 0.45,   # résidentiel — un multiplex bâti occupe au moins ça
    5: 0.45,   # commercial
    6: 0.45,   # services
}
SEUIL_SOUS_UTILISATION_DEFAUT = 0.40


def intensite_sous_utilisation(immeuble: Immeuble) -> float:
    """
    Mesure à quel point le terrain est sous-construit, entre 0.0 et 1.0.

    Un terrain vague vaut 1.0 par définition. Sinon, l'intensité croît à mesure
    que le ratio bâti/terrain s'éloigne sous le seuil du groupe.
    """
    groupe = groupe_cubf(immeuble.cubf)

    if groupe in GROUPES_DEVELOPPEMENT_PUR or immeuble.cubf == CUBF_TERRAIN_VAGUE:
        return 1.0

    ratio = immeuble.ratio_occupation_sol
    if ratio is None:
        return 0.0

    seuil = SEUIL_SOUS_UTILISATION.get(groupe, SEUIL_SOUS_UTILISATION_DEFAUT)
    if ratio >= seuil:
        return 0.0

    # Interpolation linéaire : ratio nul → 1.0, ratio au seuil → 0.0.
    intensite = (seuil - ratio) / seuil

    # Bonus : un bâtiment d'un ou deux étages sur un grand terrain dans un
    # secteur à densifier est le cas d'école du redéveloppement.
    if (immeuble.nombre_etages or 0) in (1, 2) and (immeuble.superficie_terrain or 0) > 900:
        intensite = min(1.0, intensite + 0.2)

    return round(min(1.0, max(0.0, intensite)), 3)


# ─────────────────────────────────────────────────────────────────────────────
# Score
# ─────────────────────────────────────────────────────────────────────────────


def scorer(immeuble: Immeuble, criteres: Criteres = DEFAUT) -> int:
    """
    Calcule le score 0-100 et remplit `immeuble.detail_score`.

    Chaque signal présent rapporte `poids × intensité`. L'absence d'un signal
    vaut zéro et jamais un malus : on classe des occasions, on ne juge pas les
    immeubles.
    """
    detail: dict[str, float] = {}

    # La sous-utilisation est calculée ici plutôt que collectée : elle se déduit
    # entièrement du rôle, sans source externe.
    intensite_sous_util = intensite_sous_utilisation(immeuble)
    if intensite_sous_util > 0 and not immeuble.a_signal("sous_utilisation"):
        from core.foncier.modele import Signal

        ratio = immeuble.ratio_occupation_sol
        libelle = (
            "Terrain vague ou non exploité"
            if intensite_sous_util >= 1.0
            else f"Ratio bâti/terrain de {ratio:.2f}" if ratio is not None
            else "Terrain sous-utilisé"
        )
        immeuble.ajouter_signal(
            Signal(
                cle="sous_utilisation",
                libelle=libelle,
                source=f"role_{immeuble.millesime or '?'}",
                intensite=intensite_sous_util,
            )
        )

    # La détention longue se déduit de la date d'acquisition quand elle est
    # connue (enrichissement payant). Sans elle, le signal reste absent — c'est
    # exactement pourquoi l'enrichissement se déclenche au-dessus du seuil.
    annees = immeuble.annees_detention
    if annees is not None and annees >= criteres.detention_seuil and not immeuble.a_signal(
        "detention_longue"
    ):
        from core.foncier.modele import Signal

        # Intensité plafonnée à 40 ans : au-delà, le signal ne se renforce plus.
        intensite = min(1.0, annees / 40)
        immeuble.ajouter_signal(
            Signal(
                cle="detention_longue",
                libelle=f"Même propriétaire depuis {annees} ans",
                source="registre_foncier",
                date_signal=immeuble.date_acquisition,
                intensite=intensite,
            )
        )

    # Agrégation.
    for poids in POIDS:
        signaux = [s for s in immeuble.signaux if s.cle == poids.cle]
        if not signaux:
            continue
        # Un signal par clé compte : on retient le plus intense.
        intensite = max(min(1.0, max(0.0, s.intensite)) for s in signaux)
        points = poids.points * intensite
        detail[poids.cle] = round(points, 1)

    total = round(sum(detail.values()))

    # Signaux orphelins : une clé qui ne correspond à aucun poids serait
    # silencieusement ignorée. On le dit plutôt que de la perdre. Les signaux
    # informatifs déclarés (mise en marché, etc.) sont exemptés — ils portent
    # un fait d'état, pas un indice de motivation.
    for signal in immeuble.signaux:
        if signal.cle not in POIDS_PAR_CLE and signal.cle not in SIGNAUX_INFORMATIFS:
            log.warning(
                "scoring: signal '%s' sans poids défini (immeuble %s) — ignoré",
                signal.cle,
                immeuble.adresse or immeuble.lot,
            )

    immeuble.detail_score = detail
    immeuble.score = min(total, SCORE_MAX)
    return immeuble.score


def expliquer(immeuble: Immeuble) -> str:
    """
    Rend le score lisible. Utilisé dans les briefs et pour vérifier à la main
    qu'un dossier a été noté pour les bonnes raisons.
    """
    if not immeuble.detail_score:
        return "Non scoré."

    lignes = [f"Score {immeuble.score}/{SCORE_MAX}"]
    for cle, points in sorted(immeuble.detail_score.items(), key=lambda kv: -kv[1]):
        poids = POIDS_PAR_CLE.get(cle)
        libelle = poids.libelle if poids else cle
        maximum = poids.points if poids else "?"
        signaux = [s for s in immeuble.signaux if s.cle == cle]
        precision = f" — {signaux[0].libelle}" if signaux else ""
        lignes.append(f"  · {libelle} : {points}/{maximum}{precision}")

    manquants = [p.libelle for p in POIDS if p.cle not in immeuble.detail_score]
    if manquants:
        lignes.append(f"  Signaux absents : {', '.join(manquants)}")

    return "\n".join(lignes)


def classer(
    immeubles: list[Immeuble], criteres: Criteres = DEFAUT
) -> tuple[list[Immeuble], list[Rejet]]:
    """
    Filtre, score et trie une collection d'immeubles.

    Returns:
        (retenus triés par score décroissant, rejets avec leur motif)
    """
    retenus: list[Immeuble] = []
    rejets: list[Rejet] = []

    for immeuble in immeubles:
        try:
            ok, motif = eligible(immeuble, criteres)
        except Exception as e:  # une donnée aberrante ne doit pas tuer la passe
            log.warning("scoring: immeuble illisible ignoré (%s)", e)
            continue

        if not ok:
            try:
                identifiant = immeuble.identifiant
            except ValueError:
                identifiant = "?"
            rejets.append(Rejet(identifiant, immeuble.adresse, motif))
            continue

        scorer(immeuble, criteres)
        retenus.append(immeuble)

    retenus.sort(key=lambda i: (-i.score, -(i.valeur_totale or 0)))
    return retenus, rejets


def resume_rejets(rejets: list[Rejet], limite: int = 8) -> str:
    """
    Agrège les motifs de rejet. Sans ça, un filtre trop serré passe inaperçu et
    on croit que le marché est vide alors que c'est le code qui l'est.
    """
    if not rejets:
        return "Aucun rejet."

    # Regroupement par motif générique : les valeurs numériques varient d'un
    # immeuble à l'autre et noieraient le décompte.
    familles = (
        ("logement", "nombre de logements insuffisant"),
        ("sous la bande", "valeur sous la bande de prix"),
        ("au-dessus de la bande", "valeur au-dessus de la bande de prix"),
        ("aucune valeur", "aucune valeur au rôle"),
        ("usage hors thèse", "usage hors thèse"),
        ("hors périmètre", "hors périmètre géographique"),
        ("code d'utilisation", "code d'utilisation illisible"),
        ("terrains exclus", "terrains exclus par les critères"),
    )

    compteur: dict[str, int] = {}
    for rejet in rejets:
        cle = rejet.motif
        for motif_partiel, etiquette in familles:
            if motif_partiel in rejet.motif:
                cle = etiquette
                break
        compteur[cle] = compteur.get(cle, 0) + 1

    lignes = [f"{len(rejets)} immeuble(s) écarté(s) :"]
    for motif, n in sorted(compteur.items(), key=lambda kv: -kv[1])[:limite]:
        lignes.append(f"  · {motif} — {n}")
    return "\n".join(lignes)
