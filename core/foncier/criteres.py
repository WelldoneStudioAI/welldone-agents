"""
core/foncier/criteres.py — La thèse d'acquisition, encodée.

SOURCE DE VÉRITÉ UNIQUE. Tout le reste du moteur lit ici : le scoring, les
filtres, les seuils financiers. Pour changer la stratégie d'acquisition, on
modifie ce fichier et rien d'autre.

Thèse de référence (Cedrik, juillet 2026) :
  · Commercial et multirésidentiel
  · 1 M$ à 5 M$
  · NOI 7 à 8 %
  · Potentiel de développement — densification, modification de zonage
  · Secteurs TOD et zones PPU
  · Propriétaires détenant depuis plus de 25 ans
  · Préavis d'exercice (via JLR)
  · SEAO pour les projets de développement des villes
  · Centris/MLS en parallèle comme baseline de marché
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# 1. Filtres éliminatoires — un immeuble qui échoue ici n'est jamais scoré
# ─────────────────────────────────────────────────────────────────────────────

PRIX_MIN = 1_000_000
PRIX_MAX = 5_000_000

# Marge de tolérance sous/au-dessus de la bande : la valeur au rôle est
# systématiquement décalée du prix de marché (souvent 10-25 % sous). On élargit
# donc la fenêtre de capture pour ne pas rater des immeubles qui se
# transigeraient dans la bande.
PRIX_TOLERANCE = 0.25

PRIX_CAPTURE_MIN = int(PRIX_MIN * (1 - PRIX_TOLERANCE))   # 750 000 $
PRIX_CAPTURE_MAX = int(PRIX_MAX * (1 + PRIX_TOLERANCE))   # 6 250 000 $

# Nombre de logements à partir duquel un immeuble résidentiel devient
# « multirésidentiel » au sens du financement commercial (SCHL / prêt commercial).
LOGEMENTS_MIN_MULTIRESIDENTIEL = 5

# ─────────────────────────────────────────────────────────────────────────────
# 2. Codes d'utilisation (CUBF) — Manuel d'évaluation foncière du Québec
# ─────────────────────────────────────────────────────────────────────────────
# Le CUBF est hiérarchique : le PREMIER CHIFFRE désigne le groupe. On filtre par
# groupe plutôt que par code à 4 chiffres, parce que les codes fins varient d'un
# millésime et d'une municipalité à l'autre alors que les groupes sont stables.
#
# ⚠️ Les codes fins listés en commentaire sont indicatifs. Avant la première mise
#    en production sur une nouvelle municipalité, valider avec :
#      python dispatch.py foncier cubf --municipalite "Montréal"
#    qui affiche la distribution réelle des codes du millésime chargé.

GROUPE_RESIDENTIEL   = 1  # 1000-1999 — logements, maisons de chambres, RPA
GROUPE_INDUSTRIEL    = 2  # 2000-3999 — industrie manufacturière
GROUPE_TRANSPORT     = 4  # 4000-4999 — transport, communication, services publics
GROUPE_COMMERCIAL    = 5  # 5000-5999 — commerce de détail et de gros
GROUPE_SERVICES      = 6  # 6000-6999 — services (bureaux, professionnels)
GROUPE_CULTUREL      = 7  # 7000-7999 — culturel, récréatif, loisirs
GROUPE_RESSOURCES    = 8  # 8000-8999 — production et extraction
GROUPE_NON_EXPLOITE  = 9  # 9000-9999 — immeubles non exploités, étendues d'eau

# Groupes retenus par la thèse.
GROUPES_COMMERCIAL = {GROUPE_COMMERCIAL, GROUPE_SERVICES}
GROUPES_CIBLES = {GROUPE_RESIDENTIEL} | GROUPES_COMMERCIAL

# Terrains vagues et immeubles non exploités : hors thèse en tant que revenu
# (aucun NOI), mais ce sont les meilleurs candidats « potentiel de développement »
# pur. On les garde dans une filière séparée.
GROUPES_DEVELOPPEMENT_PUR = {GROUPE_NON_EXPLOITE}

# Code d'un terrain non aménagé et non exploité — le signal de redéveloppement
# le plus direct qui existe dans le rôle.
CUBF_TERRAIN_VAGUE = 9100


def groupe_cubf(code: int | str | None) -> int | None:
    """Retourne le groupe (premier chiffre) d'un code CUBF, ou None si invalide."""
    if code is None:
        return None
    try:
        valeur = int(str(code).strip()[:4])
    except (ValueError, TypeError):
        return None
    if valeur <= 0:
        return None
    return int(str(valeur)[0])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Cibles financières
# ─────────────────────────────────────────────────────────────────────────────

NOI_CIBLE_MIN = 0.07   # 7 %
NOI_CIBLE_MAX = 0.08   # 8 %

# En dessous de ce taux de capitalisation estimé, le dossier n'est pas montré
# même s'il coche tous les autres critères — sauf s'il se qualifie sur le
# potentiel de développement seul (terrain vague, TOD, PPU).
CAP_RATE_PLANCHER = 0.05

# Dépenses d'exploitation en % des revenus bruts, par type d'immeuble.
# Sert à estimer le NOI quand on n'a pas les états financiers réels.
RATIO_DEPENSES = {
    "multiresidentiel": 0.40,
    "commercial": 0.30,      # baux nets : le locataire assume une partie
    "mixte": 0.35,
}

# Taux d'inoccupation présumé pour l'estimation.
TAUX_INOCCUPATION = 0.03

# ─────────────────────────────────────────────────────────────────────────────
# 4. Signaux de motivation du vendeur — les poids du scoring
# ─────────────────────────────────────────────────────────────────────────────
# Total = 100. Un signal absent vaut 0, jamais négatif : on classe des occasions,
# on ne pénalise pas l'absence d'information.


@dataclass(frozen=True)
class Poids:
    """Poids d'un signal dans le score final, avec sa justification."""

    cle: str
    points: int
    libelle: str
    justification: str


POIDS: tuple[Poids, ...] = (
    Poids(
        cle="detention_longue",
        points=25,
        libelle="Détention de 25 ans et plus",
        justification=(
            "Critère explicite de la thèse. Un propriétaire de longue date a un "
            "gain en capital important, souvent un immeuble payé, et une "
            "probabilité de sortie liée à l'âge ou à la succession."
        ),
    ),
    Poids(
        cle="aire_tod",
        points=20,
        libelle="Situé dans une aire TOD",
        justification=(
            "Le PMAD engage la CMM à diriger 40 % des nouveaux ménages "
            "2011-2031 vers les aires TOD. La densification y est une politique "
            "publique, pas un pari."
        ),
    ),
    Poids(
        cle="zone_ppu",
        points=15,
        libelle="Zone PPU active ou annoncée",
        justification=(
            "Un programme particulier d'urbanisme précède la modification de "
            "zonage. C'est la fenêtre où le potentiel n'est pas encore dans le prix."
        ),
    ),
    Poids(
        cle="sous_utilisation",
        points=15,
        libelle="Terrain sous-utilisé",
        justification=(
            "Faible ratio bâti/terrain, ou peu d'étages par rapport au secteur : "
            "l'écart entre le construit et le constructible est la marge de "
            "développement."
        ),
    ),
    Poids(
        cle="detresse",
        points=15,
        libelle="Signal de détresse financière",
        justification=(
            "Préavis d'exercice, vente pour taxes, arriérés. Le signal le plus "
            "fort qui existe sur la motivation du vendeur."
        ),
    ),
    Poids(
        cle="seao_proximite",
        points=10,
        libelle="Investissement public à proximité",
        justification=(
            "Un appel d'offres SEAO pour de l'infrastructure, du transport ou du "
            "réaménagement annonce une revalorisation du secteur avant le marché."
        ),
    ),
)

POIDS_PAR_CLE: dict[str, Poids] = {p.cle: p for p in POIDS}
SCORE_MAX = sum(p.points for p in POIDS)

# Signaux reconnus mais non notés. Ce sont des FAITS d'état, pas des indices de
# motivation : les inclure au score fausserait l'échelle, les ignorer perdrait
# de l'information.
#
# « mise_en_marche » en est l'exemple : qu'un immeuble soit sur Centris ne dit
# rien de plus sur la motivation du vendeur — le score l'a déjà mesurée. Ça dit
# que le dossier est actionnable MAINTENANT, ce qui relève du statut.
SIGNAUX_INFORMATIFS: frozenset[str] = frozenset({
    "mise_en_marche",
    "retire_du_marche",
})

# Seuil au-delà duquel un dossier justifie de dépenser pour l'enrichissement
# payant (registre foncier / JLR). C'est le robinet à dépenses du système.
SEUIL_ENRICHISSEMENT = 45

# Seuil au-delà duquel un dossier est poussé à JP (Telegram + Notion).
SEUIL_ALERTE = 60

# ─────────────────────────────────────────────────────────────────────────────
# 5. Géographie — aires TOD
# ─────────────────────────────────────────────────────────────────────────────
# La CMM délimite les aires TOD sur un rayon de 1 km (ou 500 m) autour des
# points d'accès au réseau de transport collectif métropolitain structurant.

TOD_RAYON_COEUR_M = 500      # cœur de l'aire : plein pointage
TOD_RAYON_ETENDU_M = 1000    # périphérie : pointage partiel
TOD_RATIO_PERIPHERIE = 0.6   # 60 % des points en zone étendue

DETENTION_SEUIL_ANNEES = 25

# ─────────────────────────────────────────────────────────────────────────────
# 6. SEAO — mots-clés des avis annonçant une revalorisation de secteur
# ─────────────────────────────────────────────────────────────────────────────
# Appliqués sur le titre et la description des avis d'appel d'offres.

SEAO_MOTS_CLES = (
    "densification",
    "requalification",
    "revitalisation",
    "programme particulier d'urbanisme",
    "ppu",
    "plan d'urbanisme",
    "modification de zonage",
    "règlement de zonage",
    "réaménagement",
    "reamenagement",
    "aire tod",
    "transit oriented",
    "pôle d'échange",
    "station de métro",
    "srb",
    "rem",
    "tramway",
    "prolongement",
    "infrastructure souterraine",
    "réfection de rue",
    "refection de rue",
    "place publique",
    "écoquartier",
    "ecoquartier",
    "logement social",
    "logement abordable",
)

# Un avis SEAO n'est pertinent que s'il émane d'un donneur d'ordre municipal ou
# de transport — pas d'un ministère sans ancrage territorial.
SEAO_ORGANISMES_PERTINENTS = (
    "ville de",
    "municipalité",
    "municipalite",
    "mrc ",
    "arrondissement",
    "société de transport",
    "societe de transport",
    "stm",
    "rtl",
    "stl",
    "exo",
    "artm",
    "communauté métropolitaine",
    "communaute metropolitaine",
)

# Rayon autour du secteur d'un avis SEAO où l'on considère qu'un immeuble
# bénéficie du signal.
SEAO_RAYON_M = 1500

# Fenêtre de pertinence d'un avis : au-delà, le marché a déjà intégré la nouvelle.
SEAO_FENETRE_JOURS = 540

# ─────────────────────────────────────────────────────────────────────────────
# 7. Secteurs prioritaires
# ─────────────────────────────────────────────────────────────────────────────
# Vide = tout le Québec. Renseigner pour concentrer l'effort (et les dépenses
# d'enrichissement) sur un territoire donné.

MUNICIPALITES_CIBLES: tuple[str, ...] = ()


@dataclass
class Criteres:
    """
    Instantané mutable de la thèse, pour les cas où l'on veut scanner avec des
    paramètres différents des valeurs par défaut sans toucher au module.

        c = Criteres()
        c.prix_max = 8_000_000
        moteur.scanner(criteres=c)
    """

    prix_min: int = PRIX_MIN
    prix_max: int = PRIX_MAX
    prix_tolerance: float = PRIX_TOLERANCE
    logements_min: int = LOGEMENTS_MIN_MULTIRESIDENTIEL
    groupes_cibles: set[int] = field(default_factory=lambda: set(GROUPES_CIBLES))
    inclure_terrains: bool = True
    noi_cible_min: float = NOI_CIBLE_MIN
    noi_cible_max: float = NOI_CIBLE_MAX
    cap_rate_plancher: float = CAP_RATE_PLANCHER
    detention_seuil: int = DETENTION_SEUIL_ANNEES
    seuil_enrichissement: int = SEUIL_ENRICHISSEMENT
    seuil_alerte: int = SEUIL_ALERTE
    municipalites: tuple[str, ...] = MUNICIPALITES_CIBLES

    @property
    def capture_min(self) -> int:
        return int(self.prix_min * (1 - self.prix_tolerance))

    @property
    def capture_max(self) -> int:
        return int(self.prix_max * (1 + self.prix_tolerance))


DEFAUT = Criteres()
