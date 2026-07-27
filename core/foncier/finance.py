"""
core/foncier/finance.py — Estimation du NOI et du taux de capitalisation.

⚠️ CE MODULE PRODUIT DES ESTIMATIONS DE TRI, PAS DES CHIFFRES D'ACQUISITION.

Le rôle d'évaluation ne contient aucun revenu. On reconstruit donc un NOI
approximatif à partir du nombre de logements ou de la superficie locative, pour
CLASSER les dossiers entre eux. Aucune offre ne se fait sur ces chiffres — ils
servent à décider quels dossiers méritent qu'on demande les états financiers
réels au vendeur.

Chaque estimation porte son niveau de confiance. Un dossier en confiance
« faible » qui affiche 9 % n'est pas meilleur qu'un dossier en confiance
« moyenne » à 7,5 % : il est juste moins connu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.foncier.criteres import (
    RATIO_DEPENSES,
    TAUX_INOCCUPATION,
)
from core.foncier.modele import Immeuble

PI2_PAR_M2 = 10.7639

# ─────────────────────────────────────────────────────────────────────────────
# Hypothèses de revenus
# ─────────────────────────────────────────────────────────────────────────────
# À réviser annuellement à partir de l'Enquête sur les logements locatifs de la
# SCHL (publiée chaque hiver) et des rapports de marché commerciaux du secteur.
# Ces valeurs sont des ordres de grandeur assumés, pas des données mesurées.

LOYER_MENSUEL_MOYEN_DEFAUT = 1_250.0

LOYER_MENSUEL_PAR_MARCHE: dict[str, float] = {
    "montréal": 1_450.0,
    "montreal": 1_450.0,
    "laval": 1_400.0,
    "longueuil": 1_350.0,
    "québec": 1_200.0,
    "quebec": 1_200.0,
    "gatineau": 1_300.0,
    "sherbrooke": 1_050.0,
    "trois-rivières": 950.0,
    "trois-rivieres": 950.0,
    "saguenay": 900.0,
}

# Loyer commercial annuel au pied carré, net.
LOYER_COMMERCIAL_PI2_DEFAUT = 18.0

LOYER_COMMERCIAL_PI2_PAR_MARCHE: dict[str, float] = {
    "montréal": 24.0,
    "montreal": 24.0,
    "laval": 20.0,
    "longueuil": 19.0,
    "québec": 18.0,
    "quebec": 18.0,
}


def _cle_marche(municipalite: str) -> str:
    return (municipalite or "").strip().lower()


def loyer_mensuel_moyen(municipalite: str) -> float:
    return LOYER_MENSUEL_PAR_MARCHE.get(_cle_marche(municipalite), LOYER_MENSUEL_MOYEN_DEFAUT)


def loyer_commercial_pi2(municipalite: str) -> float:
    return LOYER_COMMERCIAL_PI2_PAR_MARCHE.get(
        _cle_marche(municipalite), LOYER_COMMERCIAL_PI2_DEFAUT
    )


@dataclass
class Estimation:
    """Résultat d'une estimation financière, avec sa traçabilité."""

    revenu_brut: Optional[float]
    revenu_effectif: Optional[float]
    depenses: Optional[float]
    noi: Optional[float]
    cap_rate: Optional[float]
    confiance: str           # aucune | faible | moyenne | forte
    methode: str             # description lisible du calcul
    hypotheses: dict

    def dans_la_cible(self, minimum: float, maximum: float) -> bool:
        return self.cap_rate is not None and minimum <= self.cap_rate <= maximum


def estimer(
    immeuble: Immeuble,
    prix: Optional[float] = None,
    revenu_brut_reel: Optional[float] = None,
    depenses_reelles: Optional[float] = None,
) -> Estimation:
    """
    Estime le NOI et le taux de capitalisation d'un immeuble.

    Args:
        immeuble: le candidat, tel que normalisé depuis le rôle
        prix: prix demandé si connu ; à défaut, la valeur au rôle
        revenu_brut_reel: revenus bruts réels si le vendeur les a fournis
        depenses_reelles: dépenses d'exploitation réelles

    Returns:
        Une Estimation dont la confiance dit à quel point y croire.
    """
    prix_retenu = prix or immeuble.valeur_totale
    type_immeuble = immeuble.type_immeuble
    hypotheses: dict = {"type": type_immeuble, "prix_retenu": prix_retenu}

    # ── Cas 1 : chiffres réels fournis ──────────────────────────────────────
    if revenu_brut_reel is not None:
        revenu_effectif = revenu_brut_reel * (1 - TAUX_INOCCUPATION)
        if depenses_reelles is not None:
            depenses = depenses_reelles
            confiance = "forte"
            methode = "États financiers fournis par le vendeur"
        else:
            ratio = RATIO_DEPENSES.get(type_immeuble, 0.35)
            depenses = revenu_effectif * ratio
            confiance = "moyenne"
            methode = f"Revenus réels, dépenses estimées à {ratio:.0%}"
            hypotheses["ratio_depenses"] = ratio
        noi = revenu_effectif - depenses
        return Estimation(
            revenu_brut=revenu_brut_reel,
            revenu_effectif=revenu_effectif,
            depenses=depenses,
            noi=noi,
            cap_rate=(noi / prix_retenu) if prix_retenu else None,
            confiance=confiance,
            methode=methode,
            hypotheses=hypotheses,
        )

    # ── Cas 2 : reconstruction à partir du rôle ─────────────────────────────
    revenu_brut = 0.0
    composantes: list[str] = []
    confiance = "aucune"

    if immeuble.est_multiresidentiel and immeuble.nombre_logements:
        loyer = loyer_mensuel_moyen(immeuble.municipalite)
        revenu_brut += immeuble.nombre_logements * loyer * 12
        composantes.append(f"{immeuble.nombre_logements} logements × {loyer:,.0f} $/mois")
        hypotheses["loyer_mensuel"] = loyer
        confiance = "moyenne"

    if immeuble.est_commercial and immeuble.superficie_batiment:
        taux = loyer_commercial_pi2(immeuble.municipalite)
        pi2 = immeuble.superficie_batiment * PI2_PAR_M2
        revenu_brut += pi2 * taux
        composantes.append(f"{pi2:,.0f} pi² × {taux:,.0f} $/pi²")
        hypotheses["loyer_pi2"] = taux
        hypotheses["superficie_pi2"] = round(pi2)
        # La superficie locative réelle est toujours inférieure à la superficie
        # bâtie (circulations, mécanique). L'estimation est donc optimiste.
        confiance = "faible" if confiance == "aucune" else confiance

    if revenu_brut <= 0:
        return Estimation(
            revenu_brut=None,
            revenu_effectif=None,
            depenses=None,
            noi=None,
            cap_rate=None,
            confiance="aucune",
            methode=(
                "Aucun revenu estimable : ni nombre de logements, ni superficie "
                "bâtie exploitable dans le rôle"
            ),
            hypotheses=hypotheses,
        )

    ratio = RATIO_DEPENSES.get(type_immeuble, 0.35)
    revenu_effectif = revenu_brut * (1 - TAUX_INOCCUPATION)
    depenses = revenu_effectif * ratio
    noi = revenu_effectif - depenses
    hypotheses["ratio_depenses"] = ratio
    hypotheses["taux_inoccupation"] = TAUX_INOCCUPATION

    return Estimation(
        revenu_brut=revenu_brut,
        revenu_effectif=revenu_effectif,
        depenses=depenses,
        noi=noi,
        cap_rate=(noi / prix_retenu) if prix_retenu else None,
        confiance=confiance,
        methode="Reconstruction depuis le rôle — " + " + ".join(composantes),
        hypotheses=hypotheses,
    )


def appliquer(immeuble: Immeuble, **kwargs) -> Estimation:
    """Estime et écrit le résultat dans l'immeuble. Retourne l'estimation."""
    estimation = estimer(immeuble, **kwargs)
    immeuble.noi_estime = estimation.noi
    immeuble.cap_rate_estime = estimation.cap_rate
    immeuble.confiance_financiere = estimation.confiance
    return estimation


def prix_maximal(noi: float, cap_rate_cible: float) -> float:
    """
    Prix à ne pas dépasser pour atteindre un taux de capitalisation donné.

    C'est le calcul qui transforme la thèse « NOI 7 à 8 % » en chiffre d'offre.
    """
    if cap_rate_cible <= 0:
        raise ValueError("Le taux de capitalisation cible doit être positif")
    return noi / cap_rate_cible
