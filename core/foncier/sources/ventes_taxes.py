"""
core/foncier/sources/ventes_taxes.py — Ventes pour défaut de paiement de taxes.

La meilleure source du système pour commencer, et de loin :

  · elle est publique par obligation légale — la MRC DOIT publier l'avis
  · elle est gratuite et destinée à être diffusée
  · le signal de motivation est maximal : le propriétaire est en défaut
  · les dates sont fixes et connues d'avance (mai-juin pour la plupart)

Environ 85 MRC publient chacune un avis annuel, en PDF le plus souvent. Le
registre ci-dessous en amorce une dizaine ; l'étendre est du travail de saisie,
pas de programmation.

Sur l'extraction : les avis sont des PDF de mise en page libre. Le parseur en
tire ce qu'il peut (lots, adresses, montants) et, quand il n'y arrive pas, il le
dit et remonte le lien du PDF plutôt que d'inventer des lignes. Un avis illisible
par la machine reste un avis à lire à la main.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from core.foncier.modele import Immeuble, Signal
from core.foncier.sources._http import (
    SourceIndisponible,
    TTL_QUOTIDIEN,
    telecharger,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceMRC:
    """Une MRC et l'endroit où elle publie son avis de vente pour taxes."""

    nom: str
    url_page: str
    region: str = ""


# Amorce vérifiée en juillet 2026. Le Québec compte environ 85 MRC : compléter
# ce registre est le meilleur retour sur temps investi de tout le projet.
REGISTRE_MRC: tuple[SourceMRC, ...] = (
    SourceMRC("MRC de Joliette", "https://mrcjoliette.qc.ca/vente-dimmeubles/", "Lanaudière"),
    SourceMRC("MRC de Témiscamingue", "https://www.mrctemiscamingue.org/vente-pour-taxes/", "Abitibi-Témiscamingue"),
    SourceMRC("MRC d'Argenteuil", "https://argenteuil.qc.ca/services/habitation/vente-pour-non-paiement-de-taxes/", "Laurentides"),
    SourceMRC("MRC de La Nouvelle-Beauce", "https://www.nouvellebeauce.com/citoyens/vente-pour-non-paiement-de-taxes/", "Chaudière-Appalaches"),
    SourceMRC("MRC de Lac-Saint-Jean-Est", "https://mrclacsaintjeanest.qc.ca/les-ventes-pour-taxes/", "Saguenay–Lac-Saint-Jean"),
    SourceMRC("MRC de Beauce-Centre", "https://www.mrcbeaucecentre.ca/mrc-beauce-centre/vente-pour-taxes/", "Chaudière-Appalaches"),
    SourceMRC("MRC de La Vallée-du-Richelieu", "https://www.mrcvr.ca/documentation/vente-pour-taxes/", "Montérégie"),
    SourceMRC("MRC de La Jacques-Cartier", "https://mrcjacques-cartier.com/la-mrc/autres/vente-des-immeubles-pour-defaut-de-paiement-de-taxes/", "Capitale-Nationale"),
    SourceMRC("MRC de Memphrémagog", "https://www.mrcmemphremagog.com/vente-pour-taxes", "Estrie"),
    SourceMRC("MRC du Haut-Saint-Laurent", "https://mrchsl.com/", "Montérégie"),
)

# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────

# Lot du cadastre rénové : 7 à 8 chiffres, souvent espacés (« 5 123 456 »).
MOTIF_LOT = re.compile(r"\b(\d{1,2}[\s ]?\d{3}[\s ]?\d{3})\b")

# Montant en dollars.
MOTIF_MONTANT = re.compile(r"(\d{1,3}(?:[\s ,]\d{3})*(?:[.,]\d{2})?)\s*\$")

# Adresse civique en début de segment.
MOTIF_ADRESSE = re.compile(
    r"\b(\d{1,5}(?:-\d{1,5})?)\s+((?:rue|avenue|av\.|boulevard|boul\.|chemin|ch\.|route|rang|"
    r"place|montée|montee|côte|cote|terrasse|impasse)\s+[^\n,;]{2,60})",
    re.IGNORECASE,
)

MOTIF_LIEN_PDF = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE)

MOTS_AVIS = ("vente", "taxe", "avis", "immeuble", "encan", "adjudication")


def _texte_pdf(chemin: Path) -> Optional[str]:
    """
    Extrait le texte d'un PDF.

    Utilise pypdf s'il est installé. À défaut, retourne None — l'appelant
    remontera le lien du PDF pour lecture manuelle plutôt que de deviner.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            log.warning(
                "foncier.ventes_taxes: pypdf absent — extraction PDF désactivée. "
                "pip install pypdf pour l'activer."
            )
            return None

    try:
        lecteur = PdfReader(str(chemin))
        return "\n".join((page.extract_text() or "") for page in lecteur.pages)
    except Exception as e:
        log.warning("foncier.ventes_taxes: PDF illisible %s (%s)", chemin.name, e)
        return None


def _nombre(texte: str) -> Optional[float]:
    nettoye = re.sub(r"[^\d,.]", "", texte).replace(" ", "")
    if not nettoye:
        return None
    # Format québécois : « 1 234,56 » → virgule décimale.
    if "," in nettoye and nettoye.rfind(",") > nettoye.rfind("."):
        nettoye = nettoye.replace(".", "").replace(",", ".")
    else:
        nettoye = nettoye.replace(",", "")
    try:
        return float(nettoye)
    except ValueError:
        return None


def analyser_texte(texte: str, mrc: str, url: str) -> list[Immeuble]:
    """
    Extrait les immeubles d'un avis de vente pour taxes.

    Chaque ligne contenant un numéro de lot est traitée comme une entrée. C'est
    grossier mais robuste : tous les avis listent les lots, aucune mise en page
    n'est commune à deux MRC.
    """
    immeubles: list[Immeuble] = []
    aujourdhui = date.today().isoformat()

    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if len(ligne) < 8:
            continue

        lots = MOTIF_LOT.findall(ligne)
        if not lots:
            continue

        lot = re.sub(r"[\s ]", " ", lots[0]).strip()

        adresse = ""
        correspondance = MOTIF_ADRESSE.search(ligne)
        if correspondance:
            adresse = f"{correspondance.group(1)} {correspondance.group(2)}".strip()

        montants = [m for m in (_nombre(x) for x in MOTIF_MONTANT.findall(ligne)) if m]
        arrieres = max(montants) if montants else None

        immeuble = Immeuble(
            lot=lot,
            adresse=adresse,
            municipalite=mrc,
            statut="nouveau",
            vu_le=aujourdhui,
        )
        immeuble.ajouter_signal(
            Signal(
                cle="detresse",
                libelle=(
                    f"Vente pour défaut de paiement de taxes — {mrc}"
                    + (f" ({arrieres:,.0f} $ d'arriérés)".replace(",", " ") if arrieres else "")
                ),
                source=f"mrc:{mrc}",
                date_signal=aujourdhui,
                url=url,
                intensite=1.0,
                donnees={"arrieres": arrieres, "ligne": ligne[:200]},
            )
        )
        immeubles.append(immeuble)

    return immeubles


def _trouver_pdf(html: str, url_page: str) -> Optional[str]:
    """Repère le lien du PDF d'avis dans une page de MRC."""
    from urllib.parse import urljoin

    candidats = MOTIF_LIEN_PDF.findall(html)
    if not candidats:
        return None

    # On privilégie les PDF dont le nom évoque un avis de vente.
    def pertinence(lien: str) -> int:
        minuscule = lien.lower()
        return sum(1 for mot in MOTS_AVIS if mot in minuscule)

    meilleur = max(candidats, key=pertinence)
    if pertinence(meilleur) == 0:
        return None
    return urljoin(url_page, meilleur)


@dataclass
class ResultatMRC:
    """Ce qu'une MRC a donné, y compris quand elle n'a rien donné."""

    mrc: str
    immeubles: list[Immeuble]
    url_avis: Optional[str] = None
    statut: str = "ok"       # ok | aucun_avis | extraction_impossible | injoignable
    detail: str = ""


def scanner_mrc(source: SourceMRC, forcer: bool = False) -> ResultatMRC:
    """Récupère et analyse l'avis courant d'une MRC."""
    if source.url_page.lower().endswith(".pdf"):
        url_pdf: Optional[str] = source.url_page
    else:
        try:
            chemin_page = telecharger(source.url_page, ttl=TTL_QUOTIDIEN, forcer=forcer)
        except SourceIndisponible as e:
            return ResultatMRC(source.nom, [], statut="injoignable", detail=str(e))

        html = chemin_page.read_text(encoding="utf-8", errors="replace")
        url_pdf = _trouver_pdf(html, source.url_page)

        if not url_pdf:
            return ResultatMRC(
                source.nom,
                [],
                statut="aucun_avis",
                detail="aucun PDF d'avis repéré sur la page (avis peut-être hors période)",
            )

    try:
        chemin_pdf = telecharger(url_pdf, ttl=TTL_QUOTIDIEN, forcer=forcer)
    except SourceIndisponible as e:
        return ResultatMRC(source.nom, [], url_pdf, "injoignable", str(e))

    texte = _texte_pdf(chemin_pdf)
    if texte is None:
        return ResultatMRC(
            source.nom,
            [],
            url_pdf,
            "extraction_impossible",
            "PDF non extractible — à lire manuellement",
        )

    immeubles = analyser_texte(texte, source.nom, url_pdf)
    if not immeubles:
        return ResultatMRC(
            source.nom,
            [],
            url_pdf,
            "extraction_impossible",
            f"aucun lot reconnu dans {len(texte)} caractères extraits",
        )

    log.info("foncier.ventes_taxes: %s — %d immeuble(s)", source.nom, len(immeubles))
    return ResultatMRC(source.nom, immeubles, url_pdf, "ok")


def scanner(
    sources: tuple[SourceMRC, ...] = REGISTRE_MRC, forcer: bool = False
) -> tuple[list[Immeuble], list[ResultatMRC]]:
    """
    Scanne toutes les MRC du registre.

    Returns:
        (immeubles trouvés, résultats détaillés par MRC — y compris les échecs)
    """
    tous: list[Immeuble] = []
    resultats: list[ResultatMRC] = []

    for source in sources:
        try:
            resultat = scanner_mrc(source, forcer=forcer)
        except Exception as e:  # une MRC qui casse ne doit pas arrêter le scan
            log.error("foncier.ventes_taxes: %s a échoué (%s)", source.nom, e, exc_info=True)
            resultat = ResultatMRC(source.nom, [], statut="injoignable", detail=str(e))

        resultats.append(resultat)
        tous.extend(resultat.immeubles)

    return tous, resultats


def rapport(resultats: list[ResultatMRC]) -> str:
    """
    Rapport de couverture, échecs compris.

    Une MRC muette n'est pas une MRC sans immeubles : c'est peut-être un PDF
    illisible ou un site en panne. Le distinguer évite de croire le marché vide.
    """
    par_statut: dict[str, list[ResultatMRC]] = {}
    for resultat in resultats:
        par_statut.setdefault(resultat.statut, []).append(resultat)

    total = sum(len(r.immeubles) for r in resultats)
    lignes = [
        f"Ventes pour taxes — {total} immeuble(s) sur {len(resultats)} MRC scannée(s)",
        "",
    ]

    for resultat in par_statut.get("ok", []):
        lignes.append(f"  ✅ {resultat.mrc} — {len(resultat.immeubles)} immeuble(s)")

    for statut, etiquette in (
        ("aucun_avis", "aucun avis en ligne"),
        ("extraction_impossible", "À LIRE À LA MAIN"),
        ("injoignable", "injoignable"),
    ):
        for resultat in par_statut.get(statut, []):
            lien = f" → {resultat.url_avis}" if resultat.url_avis else ""
            lignes.append(f"  ⚠️  {resultat.mrc} — {etiquette}{lien}")

    manquantes = par_statut.get("extraction_impossible", []) + par_statut.get("injoignable", [])
    if manquantes:
        lignes.append("")
        lignes.append(
            f"  {len(manquantes)} MRC non couverte(s) automatiquement : "
            f"leur contenu n'est PAS inclus dans les résultats ci-dessus."
        )

    return "\n".join(lignes)
