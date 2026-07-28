"""
core/foncier/montage.py — Analyse de transaction.

Ce qui se passe APRÈS avoir trouvé un immeuble. Le moteur de prospection répond
à « lequel regarder » ; ce module répond à « combien offrir, et est-ce que ça
tient debout ».

Trois calculs qui décident tout :

  · PRIX MAXIMAL — à quel prix l'immeuble atteint le NOI visé de 7-8 %.
    C'est le chiffre qui transforme la thèse en offre.
  · DSCR — le ratio de couverture de la dette. Sous 1,20, aucun prêteur
    commercial ne suit, peu importe la beauté du dossier.
  · CASHFLOW — ce qui reste dans tes poches après la dette. Un immeuble à 7 %
    financé à 6 % ne laisse presque rien : l'écart se voit ici, pas dans le
    cap rate.

⚠️ Tout part du NOI. Si le NOI est une estimation reconstruite du rôle plutôt
   que des états financiers réels, les résultats en héritent — chaque sortie
   porte donc le niveau de confiance de l'entrée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.foncier.criteres import NOI_CIBLE_MAX, NOI_CIBLE_MIN
from core.foncier.modele import Immeuble

# ─────────────────────────────────────────────────────────────────────────────
# Hypothèses de financement
# ─────────────────────────────────────────────────────────────────────────────
# Ordres de grandeur du marché commercial québécois. À réviser avec ton courtier
# hypothécaire — ce sont des points de départ, pas des taux fermes.

MISE_DE_FONDS_DEFAUT = 0.25      # 25 % — standard commercial
TAUX_INTERET_DEFAUT = 0.0575     # 5,75 %
AMORTISSEMENT_DEFAUT = 25        # ans

# Seuils des prêteurs.
DSCR_MINIMUM = 1.20              # sous ce ratio, refus quasi systématique
DSCR_CONFORTABLE = 1.35

# Frais d'acquisition, en pourcentage du prix.
FRAIS_ACQUISITION = 0.025        # notaire, inspection, arpentage, ajustements

# Droits de mutation (« taxe de bienvenue ») — barème général du Québec.
# Les municipalités peuvent ajouter des tranches supérieures : Montréal en a
# plusieurs au-delà de 500 000 $. Le calcul ci-dessous est le barème de base et
# SOUS-ESTIME donc la facture montréalaise sur les gros immeubles.
TRANCHES_MUTATION = (
    (61_500, 0.005),
    (307_800, 0.010),
    (float("inf"), 0.015),
)


def droits_mutation(prix: float) -> float:
    """
    Droits de mutation selon le barème de base du Québec.

    ⚠️ Barème général. Montréal et quelques autres villes appliquent des
       tranches supplémentaires au-delà de 500 000 $ : sur un immeuble de
       plusieurs millions, la facture réelle est sensiblement plus élevée.
       Vérifier auprès de la municipalité avant de finaliser une offre.
    """
    reste = max(0.0, prix)
    total = 0.0
    plancher = 0.0

    for plafond, taux in TRANCHES_MUTATION:
        tranche = min(reste, plafond - plancher)
        if tranche <= 0:
            break
        total += tranche * taux
        reste -= tranche
        plancher = plafond

    return total


def paiement_mensuel(capital: float, taux_annuel: float, annees: int) -> float:
    """
    Versement hypothécaire mensuel, amortissement constant.

    Note : les prêts canadiens se composent semestriellement. On utilise ici la
    composition mensuelle, plus simple et légèrement conservatrice (le versement
    calculé est marginalement supérieur au réel).
    """
    if capital <= 0:
        return 0.0
    if taux_annuel <= 0:
        return capital / (annees * 12)

    taux_mensuel = taux_annuel / 12
    n = annees * 12
    facteur = (1 + taux_mensuel) ** n
    return capital * taux_mensuel * facteur / (facteur - 1)


# ─────────────────────────────────────────────────────────────────────────────
# Montage
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Montage:
    """Un scénario d'acquisition chiffré de bout en bout."""

    nom: str
    prix: float
    noi: float

    mise_de_fonds_pct: float = MISE_DE_FONDS_DEFAUT
    taux: float = TAUX_INTERET_DEFAUT
    amortissement: int = AMORTISSEMENT_DEFAUT

    confiance_noi: str = "aucune"

    # Calculés.
    mise_de_fonds: float = 0.0
    emprunt: float = 0.0
    service_dette_annuel: float = 0.0
    cashflow_annuel: float = 0.0
    dscr: float = 0.0
    cap_rate: float = 0.0
    rendement_mise_de_fonds: float = 0.0
    frais_acquisition: float = 0.0
    mutation: float = 0.0
    liquidites_requises: float = 0.0

    def __post_init__(self) -> None:
        self.mise_de_fonds = self.prix * self.mise_de_fonds_pct
        self.emprunt = self.prix - self.mise_de_fonds
        self.service_dette_annuel = (
            paiement_mensuel(self.emprunt, self.taux, self.amortissement) * 12
        )
        self.cashflow_annuel = self.noi - self.service_dette_annuel
        self.dscr = (
            self.noi / self.service_dette_annuel if self.service_dette_annuel > 0 else 0.0
        )
        self.cap_rate = self.noi / self.prix if self.prix > 0 else 0.0
        self.frais_acquisition = self.prix * FRAIS_ACQUISITION
        self.mutation = droits_mutation(self.prix)
        self.liquidites_requises = (
            self.mise_de_fonds + self.frais_acquisition + self.mutation
        )
        self.rendement_mise_de_fonds = (
            self.cashflow_annuel / self.liquidites_requises
            if self.liquidites_requises > 0 else 0.0
        )

    # ── Jugements ───────────────────────────────────────────────────────────

    @property
    def finançable(self) -> bool:
        return self.dscr >= DSCR_MINIMUM

    @property
    def verdict(self) -> str:
        if not self.finançable:
            return f"REFUS PROBABLE — DSCR {self.dscr:.2f} sous le seuil de {DSCR_MINIMUM}"
        if self.cashflow_annuel < 0:
            return "FINANÇABLE MAIS DÉFICITAIRE — cashflow négatif"
        if self.dscr < DSCR_CONFORTABLE:
            return f"SERRÉ — DSCR {self.dscr:.2f}, peu de marge d'erreur"
        if self.cap_rate >= NOI_CIBLE_MIN:
            return "DANS LA CIBLE"
        return f"SOUS LA CIBLE — cap rate {self.cap_rate:.1%}, visé {NOI_CIBLE_MIN:.0%}"

    def to_dict(self) -> dict:
        return {
            "nom": self.nom,
            "prix": round(self.prix),
            "noi": round(self.noi),
            "mise_de_fonds_pct": self.mise_de_fonds_pct,
            "mise_de_fonds": round(self.mise_de_fonds),
            "emprunt": round(self.emprunt),
            "taux": self.taux,
            "amortissement": self.amortissement,
            "service_dette_annuel": round(self.service_dette_annuel),
            "cashflow_annuel": round(self.cashflow_annuel),
            "cashflow_mensuel": round(self.cashflow_annuel / 12),
            "dscr": round(self.dscr, 2),
            "cap_rate": round(self.cap_rate, 4),
            "rendement_mise_de_fonds": round(self.rendement_mise_de_fonds, 4),
            "frais_acquisition": round(self.frais_acquisition),
            "mutation": round(self.mutation),
            "liquidites_requises": round(self.liquidites_requises),
            "financable": self.finançable,
            "verdict": self.verdict,
            "confiance_noi": self.confiance_noi,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Le calcul qui compte : combien offrir
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Offre:
    """Fourchette d'offre dérivée de la thèse."""

    noi: float
    prix_a_7: float          # prix pour atteindre 7 %
    prix_a_8: float          # prix pour atteindre 8 %
    prix_demande: Optional[float] = None
    ecart_a_7: Optional[float] = None   # écart en % vs le prix demandé
    prix_dscr_limite: float = 0.0       # prix au-delà duquel le prêteur refuse
    confiance_noi: str = "aucune"

    def to_dict(self) -> dict:
        return {
            "noi": round(self.noi),
            "prix_a_7": round(self.prix_a_7),
            "prix_a_8": round(self.prix_a_8),
            "prix_demande": round(self.prix_demande) if self.prix_demande else None,
            "ecart_a_7": round(self.ecart_a_7, 4) if self.ecart_a_7 is not None else None,
            "prix_dscr_limite": round(self.prix_dscr_limite),
            "confiance_noi": self.confiance_noi,
        }


def prix_pour_cap_rate(noi: float, cap_rate_cible: float) -> float:
    """Prix auquel un NOI donné produit le taux de capitalisation visé."""
    if cap_rate_cible <= 0:
        raise ValueError("Le taux de capitalisation cible doit être positif")
    return noi / cap_rate_cible


def prix_maximal_finançable(
    noi: float,
    dscr_cible: float = DSCR_MINIMUM,
    mise_de_fonds_pct: float = MISE_DE_FONDS_DEFAUT,
    taux: float = TAUX_INTERET_DEFAUT,
    amortissement: int = AMORTISSEMENT_DEFAUT,
) -> float:
    """
    Prix au-delà duquel le DSCR passe sous le seuil du prêteur.

    C'est le plafond que la banque impose, indépendamment de ce que tu es prêt à
    payer. Souvent plus contraignant que le cap rate visé — d'où l'intérêt de le
    connaître avant de déposer une offre.
    """
    if noi <= 0 or dscr_cible <= 0:
        return 0.0

    service_dette_max = noi / dscr_cible
    paiement_mensuel_max = service_dette_max / 12

    # On inverse la formule d'annuité pour retrouver le capital finançable.
    if taux <= 0:
        emprunt_max = paiement_mensuel_max * amortissement * 12
    else:
        taux_mensuel = taux / 12
        n = amortissement * 12
        facteur = (1 + taux_mensuel) ** n
        emprunt_max = paiement_mensuel_max * (facteur - 1) / (taux_mensuel * facteur)

    # L'emprunt couvre (1 - mise de fonds) du prix.
    part_empruntee = 1 - mise_de_fonds_pct
    return emprunt_max / part_empruntee if part_empruntee > 0 else 0.0


def calculer_offre(
    noi: float,
    prix_demande: Optional[float] = None,
    confiance_noi: str = "aucune",
    **financement,
) -> Offre:
    """
    Traduit un NOI en fourchette d'offre concrète.

    C'est le calcul qui transforme « NOI 7 à 8 % » en un chiffre qu'on écrit sur
    une promesse d'achat.
    """
    prix_a_7 = prix_pour_cap_rate(noi, NOI_CIBLE_MIN) if noi > 0 else 0.0
    prix_a_8 = prix_pour_cap_rate(noi, NOI_CIBLE_MAX) if noi > 0 else 0.0

    ecart = None
    if prix_demande and prix_a_7 > 0:
        ecart = (prix_a_7 - prix_demande) / prix_demande

    return Offre(
        noi=noi,
        prix_a_7=prix_a_7,
        prix_a_8=prix_a_8,
        prix_demande=prix_demande,
        ecart_a_7=ecart,
        prix_dscr_limite=prix_maximal_finançable(noi, **financement),
        confiance_noi=confiance_noi,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scénarios
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS_DEFAUT = (
    ("Conventionnel 25 %", 0.25, TAUX_INTERET_DEFAUT, 25),
    ("Effet de levier 15 %", 0.15, TAUX_INTERET_DEFAUT + 0.005, 25),
    ("Prudent 35 %", 0.35, TAUX_INTERET_DEFAUT - 0.0025, 25),
    ("Amorti 30 ans", 0.25, TAUX_INTERET_DEFAUT + 0.0025, 30),
)


def scenarios(
    prix: float, noi: float, confiance_noi: str = "aucune"
) -> list[Montage]:
    """
    Compare plusieurs structures de financement pour le même immeuble.

    L'intérêt : voir que le même dossier passe ou casse selon la mise de fonds,
    et de combien. Un DSCR à 1,18 devient 1,34 avec 10 % de plus en liquidités.
    """
    return [
        Montage(
            nom=nom,
            prix=prix,
            noi=noi,
            mise_de_fonds_pct=mise,
            taux=taux,
            amortissement=amortissement,
            confiance_noi=confiance_noi,
        )
        for nom, mise, taux, amortissement in SCENARIOS_DEFAUT
    ]


def sensibilite(
    prix: float, noi: float, variations: tuple[float, ...] = (-0.15, -0.10, 0.0, 0.10)
) -> list[dict]:
    """
    Que devient le dossier si le NOI réel diffère de l'estimation ?

    Indispensable quand le NOI vient du rôle et non des états financiers : une
    surestimation de 15 % des revenus fait passer un DSCR de 1,30 à 1,10, et le
    dossier de « finançable » à « refusé ».
    """
    resultats = []
    for variation in variations:
        noi_ajuste = noi * (1 + variation)
        montage = Montage(
            nom=f"NOI {variation:+.0%}",
            prix=prix,
            noi=noi_ajuste,
            confiance_noi="scénario",
        )
        resultats.append(
            {
                "variation": variation,
                "noi": round(noi_ajuste),
                "cap_rate": round(montage.cap_rate, 4),
                "dscr": round(montage.dscr, 2),
                "cashflow_annuel": round(montage.cashflow_annuel),
                "financable": montage.finançable,
            }
        )
    return resultats


# ─────────────────────────────────────────────────────────────────────────────
# Analyse complète d'un immeuble
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Analyse:
    """Dossier d'analyse financière complet d'un immeuble."""

    immeuble: Immeuble
    offre: Offre
    montages: list[Montage] = field(default_factory=list)
    sensibilites: list[dict] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "identifiant": self.immeuble.identifiant,
            "adresse": self.immeuble.adresse,
            "municipalite": self.immeuble.municipalite,
            "score": self.immeuble.score,
            "offre": self.offre.to_dict(),
            "montages": [m.to_dict() for m in self.montages],
            "sensibilites": self.sensibilites,
            "avertissements": self.avertissements,
        }


def analyser(
    immeuble: Immeuble,
    prix: Optional[float] = None,
    revenu_brut_reel: Optional[float] = None,
    depenses_reelles: Optional[float] = None,
) -> Analyse:
    """
    Analyse financière complète d'un immeuble.

    Args:
        immeuble: le dossier
        prix: prix demandé ou hypothèse d'offre ; à défaut, la valeur au rôle
        revenu_brut_reel / depenses_reelles: états financiers du vendeur,
            quand ils sont disponibles — ils changent tout le niveau de confiance
    """
    from core.foncier import finance

    estimation = finance.estimer(
        immeuble,
        prix=prix,
        revenu_brut_reel=revenu_brut_reel,
        depenses_reelles=depenses_reelles,
    )

    avertissements: list[str] = []
    prix_retenu = prix or immeuble.valeur_totale or 0.0

    if estimation.noi is None or estimation.noi <= 0:
        avertissements.append(
            "NOI non estimable : ni logements ni superficie exploitable. "
            "Aucun calcul d'offre possible — demander les états financiers."
        )
        return Analyse(
            immeuble=immeuble,
            offre=Offre(noi=0, prix_a_7=0, prix_a_8=0, prix_demande=prix_retenu or None),
            avertissements=avertissements,
        )

    if estimation.confiance in ("faible", "moyenne"):
        avertissements.append(
            f"NOI reconstruit du rôle (confiance {estimation.confiance}), pas des "
            "états financiers. Les chiffres ci-dessous servent à décider s'il "
            "vaut la peine de les demander — pas à faire une offre."
        )

    if not prix:
        avertissements.append(
            "Aucun prix demandé connu : la valeur au rôle sert de substitut. "
            "L'écart rôle/marché est typiquement de 10 à 25 %."
        )

    if immeuble.municipalite and "montr" in immeuble.municipalite.lower():
        avertissements.append(
            "Montréal applique des tranches de droits de mutation supérieures au "
            "barème de base : la « taxe de bienvenue » calculée est sous-estimée."
        )

    offre = calculer_offre(
        estimation.noi, prix_retenu or None, estimation.confiance
    )

    return Analyse(
        immeuble=immeuble,
        offre=offre,
        montages=scenarios(prix_retenu, estimation.noi, estimation.confiance)
        if prix_retenu > 0 else [],
        sensibilites=sensibilite(prix_retenu, estimation.noi) if prix_retenu > 0 else [],
        avertissements=avertissements,
    )


def fiche(analyse: Analyse) -> str:
    """
    Fiche d'analyse lisible — à apporter au vendeur, au banquier ou au partenaire.
    """
    immeuble = analyse.immeuble
    offre = analyse.offre

    def dollars(montant: Optional[float]) -> str:
        if montant is None:
            return "n/d"
        return f"{montant:,.0f} $".replace(",", " ")

    lignes = [
        "═" * 62,
        f"  {immeuble.adresse or 'Adresse inconnue'}, {immeuble.municipalite}",
        f"  Score de prospection : {immeuble.score}/100",
        "═" * 62,
        "",
        "IMMEUBLE",
        f"  Lot                 : {immeuble.lot or 'n/d'}",
        f"  Usage (CUBF)        : {immeuble.cubf or 'n/d'} — {immeuble.libelle_utilisation or 'n/d'}",
        f"  Logements           : {immeuble.nombre_logements or 'n/d'}",
        f"  Année               : {immeuble.annee_construction or 'n/d'}",
        f"  Terrain / bâtiment  : {immeuble.superficie_terrain or 'n/d'} m² / "
        f"{immeuble.superficie_batiment or 'n/d'} m²",
        f"  Valeur au rôle      : {dollars(immeuble.valeur_totale)}",
        f"  Propriétaire        : {immeuble.proprietaire or 'NON ENRICHI'}",
        "  Détention           : "
        + (f"{immeuble.annees_detention} ans" if immeuble.annees_detention is not None
           else "INCONNUE"),
        "",
        "REVENUS",
        f"  NOI estimé          : {dollars(offre.noi)}  (confiance : {offre.confiance_noi})",
        "",
        "FOURCHETTE D'OFFRE",
        f"  Pour un cap de 8 %  : {dollars(offre.prix_a_8)}   ← offre d'ouverture",
        f"  Pour un cap de 7 %  : {dollars(offre.prix_a_7)}   ← limite haute",
        f"  Plafond du prêteur  : {dollars(offre.prix_dscr_limite)}   (DSCR {DSCR_MINIMUM})",
    ]

    if offre.prix_demande:
        lignes.append(f"  Prix demandé        : {dollars(offre.prix_demande)}")
        if offre.ecart_a_7 is not None:
            verdict = (
                "sous ta limite — négociable"
                if offre.ecart_a_7 >= 0
                else "AU-DESSUS de ta limite à 7 %"
            )
            lignes.append(f"  Écart               : {offre.ecart_a_7:+.1%} — {verdict}")

    if analyse.montages:
        lignes += ["", "SCÉNARIOS DE FINANCEMENT"]
        lignes.append(
            f"  {'Scénario':<22} {'Liquidités':>12} {'Cashflow/an':>12} {'DSCR':>7}  Verdict"
        )
        for montage in analyse.montages:
            lignes.append(
                f"  {montage.nom:<22} {dollars(montage.liquidites_requises):>12} "
                f"{dollars(montage.cashflow_annuel):>12} {montage.dscr:>7.2f}  "
                f"{montage.verdict}"
            )

    if analyse.sensibilites:
        lignes += ["", "SI LE NOI RÉEL DIFFÈRE DE L'ESTIMATION"]
        for scenario in analyse.sensibilites:
            marque = "✅" if scenario["financable"] else "❌"
            lignes.append(
                f"  {marque} NOI {scenario['variation']:+.0%} → "
                f"cap {scenario['cap_rate']:.1%} · DSCR {scenario['dscr']:.2f} · "
                f"cashflow {dollars(scenario['cashflow_annuel'])}"
            )

    if analyse.avertissements:
        lignes += ["", "À SAVOIR"]
        for avertissement in analyse.avertissements:
            lignes.append(f"  ⚠️  {avertissement}")

    lignes += [
        "",
        "═" * 62,
        "  Estimations produites par l'agent foncier Welldone.",
        "  À valider contre les états financiers réels avant toute offre.",
        "═" * 62,
    ]

    return "\n".join(lignes)
