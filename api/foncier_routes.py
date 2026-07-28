"""
api/foncier_routes.py — API HTTP de l'outil de prospection foncière.

Se monte sur l'application FastAPI construite dans main.py :

    from api.foncier_routes import router as foncier_router
    fastapi_app.include_router(foncier_router)

L'interface est servie à /foncier/, les données sous /foncier/api/.

Sur les connexions SQLite : uvicorn tourne dans son propre fil d'exécution et
sert plusieurs requêtes en parallèle. Chaque appel ouvre donc sa connexion et la
referme — c'est le comportement par défaut des fonctions de `store` et `suivi`,
et il ne faut pas le contourner par une connexion partagée.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/foncier", tags=["foncier"])

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

# Le secret du dashboard existant sert aussi ici ; à défaut, celui des webhooks.
SECRET = (
    os.environ.get("FONCIER_SECRET")
    or os.environ.get("DASHBOARD_SECRET")
    or os.environ.get("WEBHOOK_SECRET")
    or ""
)


def _verifier(request: Request, authorization: str = "") -> None:
    """Auth par Bearer ou ?token=. Ouvert si aucun secret n'est configuré."""
    if not SECRET:
        return
    jeton = (
        authorization.replace("Bearer ", "").strip()
        or request.query_params.get("token", "")
    )
    if jeton != SECRET:
        raise HTTPException(status_code=401, detail="Token invalide")


# ─────────────────────────────────────────────────────────────────────────────
# Interface
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/", response_class=FileResponse)
async def interface(request: Request, authorization: str = Header(default="")):
    """Sert l'interface de prospection."""
    _verifier(request, authorization)
    page = DASHBOARD_DIR / "foncier.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="dashboard/foncier.html introuvable")
    return FileResponse(str(page))


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/api/stats")
async def stats(request: Request, authorization: str = Header(default="")):
    """Portrait global : compteurs, dépenses, état de configuration."""
    _verifier(request, authorization)
    from core.foncier import store
    from core.foncier.criteres import DEFAUT, POIDS, SCORE_MAX
    from core.foncier.sources import registre, ventes_taxes
    from core.foncier.sources.zonage import Zonage

    portrait = store.statistiques()
    zonage = Zonage.charger()

    return {
        "base": portrait,
        "these": {
            "prix_min": DEFAUT.prix_min,
            "prix_max": DEFAUT.prix_max,
            "capture_min": DEFAUT.capture_min,
            "capture_max": DEFAUT.capture_max,
            "noi_min": DEFAUT.noi_cible_min,
            "noi_max": DEFAUT.noi_cible_max,
            "detention_seuil": DEFAUT.detention_seuil,
            "score_max": SCORE_MAX,
            "poids": [
                {"cle": p.cle, "points": p.points, "libelle": p.libelle,
                 "justification": p.justification}
                for p in POIDS
            ],
        },
        "configuration": {
            "zonage_actif": zonage.actif,
            "zonage_detail": zonage.sources_actives,
            "registre_actif": bool(registre.fournisseurs_actifs()),
            "mrc_configurees": len(ventes_taxes.REGISTRE_MRC),
            "points_atteignables": _points_atteignables(
                zonage.actif, bool(registre.fournisseurs_actifs())
            ),
        },
    }


def _points_atteignables(zonage_actif: bool, registre_actif: bool) -> int:
    """
    Plafond réel du score selon ce qui est branché.

    Sans zonage, les 20 points d'aire TOD et les 15 de zone PPU sont hors
    d'atteinte ; sans registre, les 25 points de détention longue aussi.
    """
    total = 100
    if not zonage_actif:
        total -= 35
    if not registre_actif:
        total -= 25
    return total


@router.get("/api/dossiers")
async def dossiers(
    request: Request,
    authorization: str = Header(default=""),
    limite: int = Query(100, ge=1, le=1000),
    score_min: int = Query(0, ge=0, le=100),
    municipalite: Optional[str] = None,
    statut: Optional[str] = None,
):
    """Liste filtrable des dossiers."""
    _verifier(request, authorization)
    from core.foncier import store

    statuts = (statut,) if statut else None
    immeubles = store.meilleurs(limite, score_min, statuts, municipalite)

    return {
        "total": len(immeubles),
        "dossiers": [
            {
                "identifiant": i.identifiant,
                "adresse": i.adresse,
                "municipalite": i.municipalite,
                "score": i.score,
                "detail_score": i.detail_score,
                "valeur_totale": i.valeur_totale,
                "cap_rate_estime": i.cap_rate_estime,
                "noi_estime": i.noi_estime,
                "confiance_financiere": i.confiance_financiere,
                "nombre_logements": i.nombre_logements,
                "cubf": i.cubf,
                "type_immeuble": i.type_immeuble,
                "proprietaire": i.proprietaire,
                "annees_detention": i.annees_detention,
                "statut": i.statut,
                "latitude": i.latitude,
                "longitude": i.longitude,
                "vu_le": i.vu_le,
                "signaux": [s.to_dict() for s in i.signaux],
            }
            for i in immeubles
        ],
    }


@router.get("/api/dossier/{identifiant}")
async def dossier(
    identifiant: str, request: Request, authorization: str = Header(default="")
):
    """Fiche complète : score expliqué, signaux, contacts, historique, analyse."""
    _verifier(request, authorization)
    from core.foncier import montage, scoring, store, suivi

    immeuble = store.charger(identifiant)
    if immeuble is None:
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    analyse = montage.analyser(immeuble)

    return {
        "immeuble": immeuble.to_dict(),
        "explication_score": scoring.expliquer(immeuble),
        "analyse": analyse.to_dict(),
        "fiche_texte": montage.fiche(analyse),
        "contacts": [c.to_dict() for c in suivi.contacts_du_dossier(identifiant)],
        "historique": [h.to_dict() for h in suivi.historique(identifiant)],
    }


@router.get("/api/carte")
async def carte(
    request: Request,
    authorization: str = Header(default=""),
    score_min: int = Query(0, ge=0, le=100),
    limite: int = Query(500, ge=1, le=2000),
):
    """
    Points géolocalisés et zones de zonage, pour la vue carte.

    Les dossiers sans coordonnées sont comptés à part plutôt qu'omis en
    silence : une carte vide parce que le rôle chargé n'était pas géoréférencé
    doit se voir.
    """
    _verifier(request, authorization)
    from core.foncier import store

    immeubles = store.meilleurs(limite, score_min)
    avec_coordonnees = [i for i in immeubles if i.latitude and i.longitude]

    return {
        "points": [
            {
                "identifiant": i.identifiant,
                "lat": i.latitude,
                "lon": i.longitude,
                "score": i.score,
                "adresse": i.adresse,
                "municipalite": i.municipalite,
                "valeur_totale": i.valeur_totale,
                "statut": i.statut,
            }
            for i in avec_coordonnees
        ],
        "sans_coordonnees": len(immeubles) - len(avec_coordonnees),
        "total": len(immeubles),
    }


@router.get("/api/pipeline")
async def pipeline(request: Request, authorization: str = Header(default="")):
    """Vue kanban par statut."""
    _verifier(request, authorization)
    from core.foncier import suivi

    return suivi.tableau_pipeline()


@router.get("/api/mouvements")
async def mouvements(
    request: Request,
    authorization: str = Header(default=""),
    jours: int = Query(14, ge=1, le=365),
):
    """Ce qui a bougé et mérite une action aujourd'hui."""
    _verifier(request, authorization)
    from core.foncier import suivi

    return {"mouvements": [m.to_dict() for m in suivi.mouvements(jours)]}


@router.get("/api/relances")
async def relances(request: Request, authorization: str = Header(default="")):
    """Relances dues ou en retard."""
    _verifier(request, authorization)
    from core.foncier import suivi

    return {"relances": suivi.relances_dues()}


@router.get("/api/fiche/{identifiant}", response_class=PlainTextResponse)
async def fiche_imprimable(
    identifiant: str, request: Request, authorization: str = Header(default="")
):
    """Fiche d'analyse en texte — à imprimer ou à envoyer au banquier."""
    _verifier(request, authorization)
    from core.foncier import montage, store

    immeuble = store.charger(identifiant)
    if immeuble is None:
        raise HTTPException(status_code=404, detail="Dossier introuvable")
    return montage.fiche(montage.analyser(immeuble))


# ─────────────────────────────────────────────────────────────────────────────
# Écriture
# ─────────────────────────────────────────────────────────────────────────────


class ChangementStatut(BaseModel):
    statut: Optional[str] = None
    note: Optional[str] = None


@router.post("/api/dossier/{identifiant}/statut")
async def changer_statut(
    identifiant: str,
    payload: ChangementStatut,
    request: Request,
    authorization: str = Header(default=""),
):
    """Déplace un dossier dans le pipeline, ou lui ajoute une note."""
    _verifier(request, authorization)
    from core.foncier import store

    statuts = ("nouveau", "surveille", "enrichi", "en_vente", "contacte", "rejete")
    if payload.statut and payload.statut not in statuts:
        raise HTTPException(
            status_code=400, detail=f"Statut invalide. Valides : {', '.join(statuts)}"
        )

    if not store.marquer(identifiant, payload.statut, payload.note):
        raise HTTPException(status_code=404, detail="Dossier introuvable")
    return {"ok": True}


class NouvelleInteraction(BaseModel):
    canal: str
    resume: str
    resultat: Optional[str] = None
    date_contact: Optional[str] = None


@router.post("/api/dossier/{identifiant}/interaction")
async def journaliser_interaction(
    identifiant: str,
    payload: NouvelleInteraction,
    request: Request,
    authorization: str = Header(default=""),
):
    """
    Consigne un appel, un courriel, une visite — et programme la relance.

    La relance se planifie automatiquement selon le résultat : un « intéressé »
    revient dans 5 jours, un « sans réponse » dans 14.
    """
    _verifier(request, authorization)
    from core.foncier import suivi

    try:
        return suivi.journaliser(
            suivi.Interaction(
                identifiant=identifiant,
                canal=payload.canal,
                resume=payload.resume,
                resultat=payload.resultat,
                date_contact=payload.date_contact or "",
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class NouveauContact(BaseModel):
    nom: str
    role: str = "proprietaire"
    telephone: Optional[str] = None
    courriel: Optional[str] = None
    notes: Optional[str] = None


@router.post("/api/dossier/{identifiant}/contact")
async def ajouter_contact(
    identifiant: str,
    payload: NouveauContact,
    request: Request,
    authorization: str = Header(default=""),
):
    """Rattache une personne au dossier."""
    _verifier(request, authorization)
    from core.foncier import suivi

    contact_id = suivi.ajouter_contact(
        suivi.Contact(
            identifiant=identifiant, nom=payload.nom, role=payload.role,
            telephone=payload.telephone, courriel=payload.courriel, notes=payload.notes,
        )
    )
    return {"ok": True, "contact_id": contact_id}


class NouvelleRelance(BaseModel):
    dans_jours: int = 14
    motif: str = "Relance"


@router.post("/api/dossier/{identifiant}/relance")
async def creer_relance(
    identifiant: str,
    payload: NouvelleRelance,
    request: Request,
    authorization: str = Header(default=""),
):
    """Programme une relance manuelle."""
    _verifier(request, authorization)
    from core.foncier import suivi

    return {"ok": True, "date_prevue": suivi.programmer_relance(
        identifiant, payload.dans_jours, payload.motif
    )}


@router.post("/api/relance/{relance_id}/faite")
async def cloturer_relance(
    relance_id: int, request: Request, authorization: str = Header(default="")
):
    """Marque une relance comme faite."""
    _verifier(request, authorization)
    from core.foncier import suivi

    if not suivi.marquer_relance_faite(relance_id):
        raise HTTPException(status_code=404, detail="Relance introuvable ou déjà faite")
    return {"ok": True}


class ParametresAnalyse(BaseModel):
    """Hypothèses d'analyse saisies à la main dans le calculateur."""

    prix: Optional[float] = None
    revenu_brut_reel: Optional[float] = None
    depenses_reelles: Optional[float] = None
    mise_de_fonds_pct: Optional[float] = None
    taux: Optional[float] = None
    amortissement: Optional[int] = None


@router.post("/api/dossier/{identifiant}/analyse")
async def analyser_dossier(
    identifiant: str,
    payload: ParametresAnalyse,
    request: Request,
    authorization: str = Header(default=""),
):
    """
    Recalcule l'analyse financière avec tes propres hypothèses.

    C'est le calculateur d'offre : tu entres le prix demandé et, si le vendeur
    les a fournis, les revenus réels — et tu obtiens la fourchette d'offre, les
    scénarios de financement et le point de rupture du prêteur.
    """
    _verifier(request, authorization)
    from core.foncier import montage, store

    immeuble = store.charger(identifiant)
    if immeuble is None:
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    analyse = montage.analyser(
        immeuble,
        prix=payload.prix,
        revenu_brut_reel=payload.revenu_brut_reel,
        depenses_reelles=payload.depenses_reelles,
    )

    resultat = analyse.to_dict()

    # Montage sur mesure, si des paramètres de financement sont fournis.
    if any((payload.mise_de_fonds_pct, payload.taux, payload.amortissement)):
        prix = payload.prix or immeuble.valeur_totale or 0
        if prix > 0 and analyse.offre.noi > 0:
            sur_mesure = montage.Montage(
                nom="Sur mesure",
                prix=prix,
                noi=analyse.offre.noi,
                mise_de_fonds_pct=payload.mise_de_fonds_pct or montage.MISE_DE_FONDS_DEFAUT,
                taux=payload.taux or montage.TAUX_INTERET_DEFAUT,
                amortissement=payload.amortissement or montage.AMORTISSEMENT_DEFAUT,
                confiance_noi=analyse.offre.confiance_noi,
            )
            resultat["montages"] = [sur_mesure.to_dict()] + resultat["montages"]

    resultat["fiche_texte"] = montage.fiche(analyse)
    return resultat


# ─────────────────────────────────────────────────────────────────────────────
# Déclenchement des agents depuis l'interface
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/api/commande/{commande}")
async def executer_commande(
    commande: str, request: Request, authorization: str = Header(default="")
):
    """
    Lance une commande de l'agent foncier depuis l'interface.

    Limité aux commandes de lecture et de collecte. `enrichir` est exclu
    volontairement : c'est la seule qui dépense de l'argent, et elle doit rester
    un geste délibéré en ligne de commande.
    """
    _verifier(request, authorization)
    from core.dispatcher import dispatch

    autorisees = {"scan", "seao", "marche", "top", "sources", "brief"}
    if commande not in autorisees:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Commande « {commande} » non exposée à l'interface. "
                f"Autorisées : {', '.join(sorted(autorisees))}. "
                "L'enrichissement payant se lance en ligne de commande."
            ),
        )

    try:
        contexte = dict(await request.json()) if await request.body() else None
    except Exception:
        contexte = None

    resultat = await dispatch("foncier", commande, contexte)
    return {"ok": True, "resultat": resultat}
