"""
core/foncier/sources/registre.py — Enrichissement payant (registre foncier).

C'est ici que le système dépense de l'argent, et c'est le seul endroit.

Ce que le registre foncier apporte et que rien d'autre ne donne :
  · le nom du propriétaire (caviardé dans l'open data)
  · la date d'acquisition — donc les « plus de 25 ans de détention » de la thèse
  · les hypothèques publiées
  · les préavis d'exercice

Ce qu'il faut savoir avant de coder quoi que ce soit :

  1. Le registre s'interroge PAR IMMEUBLE (lot, adresse), jamais en lot. Il n'y a
     pas de flux « tous les préavis de la semaine ». Un fournisseur commercial
     est nécessaire pour ça — c'est précisément ce que fait JLR.
  2. Les conditions d'utilisation du Registre foncier du Québec en ligne
     interdisent l'extraction automatisée massive. Ce module n'appelle donc
     JAMAIS registrefoncier.gouv.qc.ca directement : il passe par des
     fournisseurs qui ont l'entente (fonciq, JLR) ou il ne fait rien.
  3. Chaque appel coûte. Le garde-fou budgétaire ci-dessous est une limite dure,
     pas une suggestion : un scan mal calibré sur 500 000 unités d'évaluation
     coûterait des dizaines de milliers de dollars.

Aucun adaptateur n'invente de données. Sans clé configurée, l'enrichissement
retourne « non disponible » et le dossier reste au stade du scoring gratuit.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol

from core.foncier.modele import Immeuble, Signal

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

FONCIQ_API_KEY = os.environ.get("FONCIQ_API_KEY", "")
FONCIQ_BASE_URL = os.environ.get("FONCIQ_BASE_URL", "https://api.fonciq.ca/v1")

JLR_API_KEY = os.environ.get("JLR_API_KEY", "")
JLR_BASE_URL = os.environ.get("JLR_BASE_URL", "")

# Plafond de dépense par exécution, en dollars. Limite DURE.
BUDGET_PAR_PASSE = float(os.environ.get("FONCIER_BUDGET_PASSE", "25"))

# Plafond mensuel cumulé.
BUDGET_MENSUEL = float(os.environ.get("FONCIER_BUDGET_MENSUEL", "200"))

# Coût unitaire présumé d'un enrichissement : crédit du fournisseur + frais
# officiels du registre refacturés. Sert au garde-fou avant l'appel ; le coût
# réel retourné par le fournisseur le remplace ensuite.
COUT_UNITAIRE_ESTIME = float(os.environ.get("FONCIER_COUT_UNITAIRE", "2.50"))

TIMEOUT = 45


class BudgetEpuise(RuntimeError):
    """Le plafond de dépense est atteint. L'exécution s'arrête proprement."""


class FournisseurNonConfigure(RuntimeError):
    """Aucune clé d'API. L'enrichissement est simplement sauté."""


@dataclass
class Budget:
    """Compteur de dépenses avec plafond dur."""

    plafond: float = BUDGET_PAR_PASSE
    depense: float = 0.0
    appels: int = 0
    refuses: int = 0

    def autoriser(self, cout_estime: float = COUT_UNITAIRE_ESTIME) -> bool:
        """Vrai si un appel de plus tient dans le plafond."""
        if self.depense + cout_estime > self.plafond:
            self.refuses += 1
            return False
        return True

    def enregistrer(self, cout: float) -> None:
        self.depense += cout
        self.appels += 1

    def resume(self) -> str:
        texte = f"{self.depense:.2f} $ dépensés en {self.appels} appel(s) (plafond {self.plafond:.2f} $)"
        if self.refuses:
            texte += f" — {self.refuses} enrichissement(s) NON EFFECTUÉ(S), budget atteint"
        return texte


@dataclass
class Fiche:
    """Ce qu'un fournisseur retourne pour un immeuble."""

    proprietaire: Optional[str] = None
    date_acquisition: Optional[str] = None
    prix_acquisition: Optional[float] = None
    hypotheques: list[dict] = field(default_factory=list)
    preavis_exercice: Optional[dict] = None
    cout: float = 0.0
    source: str = ""
    brut: dict = field(default_factory=dict)


class Fournisseur(Protocol):
    """Contrat commun à tous les fournisseurs de données du registre."""

    nom: str

    def disponible(self) -> bool: ...

    def fiche_immeuble(self, immeuble: Immeuble) -> Fiche: ...


def _appel_json(url: str, entetes: dict, corps: Optional[dict] = None) -> dict:
    donnees = json.dumps(corps).encode("utf-8") if corps is not None else None
    requete = urllib.request.Request(url, data=donnees, headers=entetes, method="POST" if corps else "GET")
    with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# fonciq
# ─────────────────────────────────────────────────────────────────────────────


class Fonciq:
    """
    Adaptateur fonciq — consolidation registre foncier + Infolot + rôles.

    Tarification observée en juillet 2026 : à partir de 0,99 $ le crédit au
    volume, plus les frais officiels du registre (1,50 $ par document)
    refacturés à coût exact. L'API v1 était annoncée « en préparation pour les
    partenaires pilotes » : tant qu'elle n'est pas ouverte, `disponible()`
    retourne faux et le moteur continue sans enrichissement.
    """

    nom = "fonciq"

    def disponible(self) -> bool:
        return bool(FONCIQ_API_KEY)

    def fiche_immeuble(self, immeuble: Immeuble) -> Fiche:
        if not self.disponible():
            raise FournisseurNonConfigure("FONCIQ_API_KEY absente")

        if not immeuble.lot and not immeuble.adresse:
            raise ValueError("Ni lot ni adresse : impossible d'interroger le registre")

        parametres = {"lot": immeuble.lot} if immeuble.lot else {
            "adresse": immeuble.adresse,
            "municipalite": immeuble.municipalite,
        }

        try:
            reponse = _appel_json(
                f"{FONCIQ_BASE_URL}/immeubles/recherche",
                {
                    "Authorization": f"Bearer {FONCIQ_API_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                parametres,
            )
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise FournisseurNonConfigure(
                    f"fonciq a refusé la clé ({e.code}) — vérifier FONCIQ_API_KEY "
                    "et l'accès partenaire à l'API v1"
                ) from e
            raise

        return self._vers_fiche(reponse)

    def _vers_fiche(self, reponse: dict) -> Fiche:
        """
        Normalise la réponse du fournisseur.

        Les noms de champs sont lus de façon tolérante : le contrat de l'API v1
        n'était pas public au moment de l'écriture. Si un champ manque, il reste
        à None — jamais deviné.
        """
        def lire(*cles: str):
            for cle in cles:
                if cle in reponse and reponse[cle] not in (None, "", []):
                    return reponse[cle]
            return None

        return Fiche(
            proprietaire=lire("proprietaire", "owner", "titulaire"),
            date_acquisition=lire("date_acquisition", "dateAcquisition", "acquisition_date"),
            prix_acquisition=lire("prix_acquisition", "prixAcquisition", "purchase_price"),
            hypotheques=lire("hypotheques", "hypothecs") or [],
            preavis_exercice=lire("preavis_exercice", "preavisExercice"),
            cout=float(lire("cout", "cost", "credits_utilises") or COUT_UNITAIRE_ESTIME),
            source="fonciq",
            brut=reponse,
        )


# ─────────────────────────────────────────────────────────────────────────────
# JLR
# ─────────────────────────────────────────────────────────────────────────────


class JLR:
    """
    Adaptateur JLR (Equifax) — transactions et préavis d'exercice.

    C'est le fournisseur cité dans la thèse pour la recherche d'immeubles avec
    préavis d'exercice, parce qu'il offre ce que le registre ne donne pas : une
    recherche INVERSÉE, par type de document et par territoire, plutôt que
    lot par lot.

    JLR ne publie pas de contrat d'API ouvert : l'URL et le format des requêtes
    viennent de l'entente commerciale. Renseigner JLR_BASE_URL et JLR_API_KEY
    une fois le compte ouvert.
    """

    nom = "jlr"

    def disponible(self) -> bool:
        return bool(JLR_API_KEY and JLR_BASE_URL)

    def fiche_immeuble(self, immeuble: Immeuble) -> Fiche:
        if not self.disponible():
            raise FournisseurNonConfigure("JLR_API_KEY ou JLR_BASE_URL absente")

        reponse = _appel_json(
            f"{JLR_BASE_URL.rstrip('/')}/immeuble",
            {"Authorization": f"Bearer {JLR_API_KEY}", "Accept": "application/json"},
            {"lot": immeuble.lot, "adresse": immeuble.adresse},
        )
        return Fiche(
            proprietaire=reponse.get("proprietaire"),
            date_acquisition=reponse.get("date_acquisition"),
            prix_acquisition=reponse.get("prix_vente"),
            hypotheques=reponse.get("hypotheques") or [],
            preavis_exercice=reponse.get("preavis"),
            cout=float(reponse.get("cout") or COUT_UNITAIRE_ESTIME),
            source="jlr",
            brut=reponse,
        )

    def preavis_par_territoire(self, municipalites: list[str], depuis_jours: int = 90) -> list[dict]:
        """
        Recherche inversée : les préavis d'exercice publiés sur un territoire.

        C'est LA requête qui justifie l'abonnement — celle qu'on ne peut pas
        faire soi-même sur le registre public.
        """
        if not self.disponible():
            raise FournisseurNonConfigure("JLR_API_KEY ou JLR_BASE_URL absente")

        reponse = _appel_json(
            f"{JLR_BASE_URL.rstrip('/')}/preavis",
            {"Authorization": f"Bearer {JLR_API_KEY}", "Accept": "application/json"},
            {"municipalites": municipalites, "depuis_jours": depuis_jours},
        )
        return reponse.get("resultats") or []


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────


def fournisseurs_actifs() -> list[Fournisseur]:
    """Fournisseurs configurés, dans l'ordre de préférence."""
    return [f for f in (Fonciq(), JLR()) if f.disponible()]  # type: ignore[list-item]


def enrichir(
    immeubles: list[Immeuble],
    budget: Optional[Budget] = None,
    maximum: Optional[int] = None,
) -> tuple[int, Budget, list[str]]:
    """
    Enrichit une liste d'immeubles PRÉ-TRIÉE par score décroissant.

    L'appelant est responsable de n'envoyer ici que des dossiers ayant passé
    `SEUIL_ENRICHISSEMENT`. Ce module ne re-vérifie pas la pertinence : il
    protège le budget, c'est tout.

    Returns:
        (nombre enrichis, budget utilisé, avertissements)
    """
    budget = budget or Budget()
    avertissements: list[str] = []

    disponibles = fournisseurs_actifs()
    if not disponibles:
        avertissements.append(
            "Aucun fournisseur de registre configuré (FONCIQ_API_KEY / JLR_API_KEY). "
            "Le signal « détention de 25 ans et plus » — 25 points, le plus lourd de "
            "la thèse — ne peut pas être attribué."
        )
        log.warning("foncier.registre: %s", avertissements[-1])
        return 0, budget, avertissements

    candidats = immeubles[:maximum] if maximum else immeubles
    enrichis = 0

    for immeuble in candidats:
        if not budget.autoriser():
            avertissements.append(
                f"Budget atteint après {enrichis} enrichissement(s) : "
                f"{len(candidats) - enrichis} dossier(s) NON enrichi(s)."
            )
            log.warning("foncier.registre: %s", avertissements[-1])
            break

        for fournisseur in disponibles:
            try:
                fiche = fournisseur.fiche_immeuble(immeuble)
            except FournisseurNonConfigure as e:
                log.info("foncier.registre: %s indisponible (%s)", fournisseur.nom, e)
                continue
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
                log.warning(
                    "foncier.registre: %s a échoué sur %s (%s)",
                    fournisseur.nom, immeuble.adresse or immeuble.lot, e,
                )
                continue

            appliquer_fiche(immeuble, fiche)
            budget.enregistrer(fiche.cout)
            enrichis += 1
            break
        else:
            avertissements.append(
                f"Aucun fournisseur n'a répondu pour {immeuble.adresse or immeuble.lot}"
            )

    log.info("foncier.registre: %d immeuble(s) enrichi(s) — %s", enrichis, budget.resume())
    return enrichis, budget, avertissements


def appliquer_fiche(immeuble: Immeuble, fiche: Fiche) -> None:
    """Écrit une fiche fournisseur dans l'immeuble et en dérive les signaux."""
    immeuble.proprietaire = fiche.proprietaire or immeuble.proprietaire
    immeuble.date_acquisition = fiche.date_acquisition or immeuble.date_acquisition
    immeuble.prix_acquisition = fiche.prix_acquisition or immeuble.prix_acquisition
    if fiche.hypotheques:
        immeuble.hypotheques = fiche.hypotheques
    immeuble.enrichi_le = date.today().isoformat()
    immeuble.cout_enrichissement += fiche.cout
    immeuble.statut = "enrichi"

    # Le préavis d'exercice est le signal de détresse le plus fort qui existe.
    if fiche.preavis_exercice:
        details = fiche.preavis_exercice
        date_preavis = (
            details.get("date") if isinstance(details, dict) else None
        )
        immeuble.ajouter_signal(
            Signal(
                cle="detresse",
                libelle="Préavis d'exercice d'un droit hypothécaire publié",
                source=fiche.source,
                date_signal=date_preavis,
                intensite=1.0,
                donnees=details if isinstance(details, dict) else {"details": str(details)},
            )
        )

    # `scoring.scorer()` dérivera le signal de détention longue de la date
    # d'acquisition — pas besoin de le créer ici.


def diagnostic() -> str:
    """État des fournisseurs, pour `dispatch.py foncier sources`."""
    actifs = fournisseurs_actifs()
    if not actifs:
        return (
            "Registre foncier NON CONFIGURÉ — aucun enrichissement possible.\n"
            "  Conséquence : le signal « détention 25 ans et plus » (25 pts) ne peut\n"
            "  jamais être attribué, et les préavis d'exercice ne sont pas détectés.\n"
            "  Le scoring fonctionne, mais sur 75 des 100 points. La détresse\n"
            "  financière reste captable par les ventes pour taxes des MRC.\n"
            "  → FONCIQ_API_KEY  : compte fonciq (5 crédits gratuits à l'inscription)\n"
            "  → JLR_API_KEY     : entente JLR, requise pour la recherche inversée\n"
            "                      de préavis d'exercice par territoire"
        )
    return (
        f"Registre actif — {', '.join(f.nom for f in actifs)} · "
        f"budget {BUDGET_PAR_PASSE:.0f} $/passe, {BUDGET_MENSUEL:.0f} $/mois"
    )
