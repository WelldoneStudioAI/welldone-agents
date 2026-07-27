"""
core/foncier/sources/roles.py — Rôles d'évaluation foncière.

Le socle du système. Données Québec héberge 1 140 fichiers XML couvrant toutes
les municipalités du Québec, millésimes 2008 à 2026, plus une version
géoréférencée. Plusieurs villes publient en parallèle un extrait tabulaire ou
GeoJSON plus commode.

⚠️ Les noms et adresses postales des propriétaires sont CAVIARDÉS dans l'open
   data. Le rôle donne le quoi (usage, superficie, logements, valeur), jamais le
   qui. Le « qui » et la date d'acquisition viennent de `sources/registre.py`,
   qui est payant — d'où le scoring en amont.

Sur la tolérance de schéma : 1 140 fichiers produits par autant d'organismes
municipaux n'ont pas des en-têtes homogènes. Plutôt que de coder en dur les
colonnes d'une ville, on mappe par alias et on échoue bruyamment en affichant
les colonnes réellement vues quand rien ne correspond.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

from core.foncier.geo import centroide
from core.foncier.modele import Immeuble
from core.foncier.sources._http import TTL_ANNUEL, telecharger

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Alias de colonnes — du plus spécifique au plus générique
# ─────────────────────────────────────────────────────────────────────────────

ALIAS: dict[str, tuple[str, ...]] = {
    "civique_debut": ("CIVIQUE_DEBUT", "NO_CIVIQUE_DEBUT", "NOCIVIQUEDEBUT", "NO_CIVIQUE"),
    "civique_fin": ("CIVIQUE_FIN", "NO_CIVIQUE_FIN", "NOCIVIQUEFIN"),
    "nom_rue": ("NOM_RUE", "NOMRUE", "RUE", "ODONYME", "NOM_VOIE"),
    "municipalite": ("MUNICIPALITE", "NOM_MUNICIPALITE", "NOM_MUN", "MUN", "VILLE"),
    "arrondissement": ("NOM_ARROND", "ARRONDISSEMENT", "NO_ARROND_ILE_CUM", "SECTEUR"),
    "cubf": ("CODE_UTILISATION", "CUBF", "CODE_UTILISATION_BIEN_FONDS", "CODEUTILISATION"),
    "libelle_utilisation": ("LIBELLE_UTILISATION", "UTILISATION", "DESC_UTILISATION"),
    "nombre_logements": ("NOMBRE_LOGEMENT", "NOMBRE_LOGEMENTS", "NB_LOGEMENTS", "NBLOGEMENTS"),
    "nombre_etages": ("ETAGE_HORS_SOL", "NOMBRE_ETAGES", "NB_ETAGES", "NBETAGES"),
    "annee_construction": ("ANNEE_CONSTRUCTION", "ANNEECONSTRUCTION", "AN_CONSTRUCTION"),
    "superficie_terrain": ("SUPERFICIE_TERRAIN", "SUPERFICIETERRAIN", "SUP_TERRAIN", "AIRE_TERRAIN"),
    "superficie_batiment": ("SUPERFICIE_BATIMENT", "SUPERFICIEBATIMENT", "SUP_BATIMENT", "AIRE_ETAGES"),
    "matricule": ("MATRICULE83", "MATRICULE", "NO_MATRICULE", "MAT18"),
    "lot": ("NO_LOT", "NUMERO_LOT", "LOT", "CADASTRE", "NO_LOT_RENOVE"),
    "valeur_terrain": ("VALEUR_TERRAIN", "VAL_TERRAIN", "VALEURTERRAIN"),
    "valeur_batiment": ("VALEUR_BATIMENT", "VAL_BATIMENT", "VALEURBATIMENT"),
    "valeur_totale": ("VALEUR_TOTALE", "VAL_TOTALE", "VALEURIMMEUBLE", "VALEUR_IMMEUBLE"),
    "latitude": ("LATITUDE", "LAT", "Y"),
    "longitude": ("LONGITUDE", "LONG", "LON", "X"),
}

CHAMPS_ESSENTIELS = ("cubf", "municipalite")


class SchemaInconnu(ValueError):
    """Aucune colonne attendue n'a été reconnue dans la source."""


def _normaliser_cle(cle: str) -> str:
    """Insensibilise la comparaison : accents, casse, séparateurs, espaces."""
    cle = cle.strip().upper()
    cle = cle.replace("É", "E").replace("È", "E").replace("Ê", "E").replace("À", "A").replace("Ô", "O")
    return re.sub(r"[^A-Z0-9]", "", cle)


def construire_correspondance(colonnes: list[str]) -> dict[str, str]:
    """
    Associe chaque champ du modèle à la colonne réelle de la source.

    Retourne {champ_modele: colonne_source} pour les champs trouvés uniquement.
    """
    index = {_normaliser_cle(c): c for c in colonnes}
    correspondance: dict[str, str] = {}

    for champ, alias in ALIAS.items():
        for candidat in alias:
            colonne = index.get(_normaliser_cle(candidat))
            if colonne is not None:
                correspondance[champ] = colonne
                break

    return correspondance


def _nombre(valeur: Any) -> Optional[float]:
    """Convertit en nombre en tolérant les espaces, virgules et symboles."""
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        return float(valeur)
    texte = str(valeur).strip()
    if not texte or texte.lower() in ("null", "none", "n/d", "nd", "-"):
        return None
    texte = re.sub(r"[^\d,.\-]", "", texte).replace(",", ".")
    # Un nombre comme "1.234.567" vient d'un séparateur de milliers.
    if texte.count(".") > 1:
        texte = texte.replace(".", "", texte.count(".") - 1)
    try:
        return float(texte)
    except ValueError:
        return None


def _entier(valeur: Any) -> Optional[int]:
    nombre = _nombre(valeur)
    return int(nombre) if nombre is not None else None


def _texte(valeur: Any) -> str:
    if valeur is None:
        return ""
    return str(valeur).strip()


def _composer_adresse(ligne: dict, corr: dict[str, str]) -> str:
    debut = _texte(ligne.get(corr.get("civique_debut", ""), ""))
    fin = _texte(ligne.get(corr.get("civique_fin", ""), ""))
    rue = _texte(ligne.get(corr.get("nom_rue", ""), ""))

    numero = debut
    if fin and fin != debut:
        numero = f"{debut}-{fin}"

    return " ".join(p for p in (numero, rue) if p).strip()


def ligne_vers_immeuble(
    ligne: dict,
    corr: dict[str, str],
    millesime: Optional[int] = None,
    geometrie: Optional[dict] = None,
) -> Optional[Immeuble]:
    """Convertit une ligne brute en `Immeuble`. Retourne None si inexploitable."""

    def champ(nom: str) -> Any:
        colonne = corr.get(nom)
        return ligne.get(colonne) if colonne else None

    immeuble = Immeuble(
        lot=_texte(champ("lot")) or None,
        matricule=_texte(champ("matricule")) or None,
        adresse=_composer_adresse(ligne, corr),
        municipalite=_texte(champ("municipalite")),
        arrondissement=_texte(champ("arrondissement")) or None,
        cubf=_entier(champ("cubf")),
        libelle_utilisation=_texte(champ("libelle_utilisation")) or None,
        nombre_logements=_entier(champ("nombre_logements")),
        nombre_etages=_entier(champ("nombre_etages")),
        annee_construction=_entier(champ("annee_construction")),
        superficie_terrain=_nombre(champ("superficie_terrain")),
        superficie_batiment=_nombre(champ("superficie_batiment")),
        valeur_terrain=_nombre(champ("valeur_terrain")),
        valeur_batiment=_nombre(champ("valeur_batiment")),
        valeur_totale=_nombre(champ("valeur_totale")),
        latitude=_nombre(champ("latitude")),
        longitude=_nombre(champ("longitude")),
        millesime=millesime,
    )

    # Valeur totale reconstituée si seules les composantes sont fournies.
    if immeuble.valeur_totale is None:
        composantes = [v for v in (immeuble.valeur_terrain, immeuble.valeur_batiment) if v]
        if composantes:
            immeuble.valeur_totale = sum(composantes)

    # Coordonnées depuis la géométrie GeoJSON à défaut de colonnes lat/lon.
    if immeuble.latitude is None and geometrie:
        centre = centroide(geometrie)
        if centre:
            immeuble.latitude, immeuble.longitude = centre

    # Sans identité, l'immeuble ne peut ni être dédupliqué ni suivi.
    try:
        immeuble.identifiant
    except ValueError:
        return None

    return immeuble


# ─────────────────────────────────────────────────────────────────────────────
# Lecteurs par format
# ─────────────────────────────────────────────────────────────────────────────


def _lire_csv(chemin: Path) -> Iterator[tuple[dict, Optional[dict]]]:
    with open(chemin, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        echantillon = f.read(8192)
        f.seek(0)
        try:
            dialecte = csv.Sniffer().sniff(echantillon, delimiters=",;\t|")
        except csv.Error:
            dialecte = csv.excel  # type: ignore[assignment]
        for ligne in csv.DictReader(f, dialect=dialecte):
            yield ligne, None


def _lire_geojson(chemin: Path) -> Iterator[tuple[dict, Optional[dict]]]:
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        donnees = json.load(f)
    for feature in donnees.get("features", []):
        yield (feature.get("properties") or {}), feature.get("geometry")


def _lire_xml(chemin: Path) -> Iterator[tuple[dict, Optional[dict]]]:
    """
    Lecture en flux du XML des rôles (format du Manuel d'évaluation foncière).

    `iterparse` permet de traiter un fichier de plusieurs centaines de Mo sans le
    charger en mémoire. On considère comme une unité d'évaluation tout élément
    dont le nom local contient « RLUE » (unité d'évaluation) ou « UEF ».
    """
    contexte = ET.iterparse(chemin, events=("end",))
    for _, element in contexte:
        nom_local = element.tag.split("}")[-1].upper()
        if "RLUE" not in nom_local and "UEF" not in nom_local:
            continue

        ligne: dict[str, Any] = {}
        for enfant in element.iter():
            cle = enfant.tag.split("}")[-1]
            if enfant.text and enfant.text.strip():
                ligne[cle] = enfant.text.strip()
            ligne.update(enfant.attrib)

        if ligne:
            yield ligne, None
        element.clear()


LECTEURS = {
    ".csv": _lire_csv,
    ".tsv": _lire_csv,
    ".txt": _lire_csv,
    ".geojson": _lire_geojson,
    ".json": _lire_geojson,
    ".xml": _lire_xml,
}


# ─────────────────────────────────────────────────────────────────────────────
# API publique
# ─────────────────────────────────────────────────────────────────────────────


def charger_fichier(
    chemin: Path | str,
    millesime: Optional[int] = None,
    limite: Optional[int] = None,
) -> list[Immeuble]:
    """
    Charge un rôle d'évaluation depuis un fichier local.

    Args:
        chemin: CSV, GeoJSON ou XML
        millesime: année du rôle, inscrite sur chaque immeuble
        limite: ne lire que les N premières lignes (mise au point)

    Raises:
        SchemaInconnu: si aucune colonne essentielle n'est reconnue. Le message
            liste les colonnes réellement présentes pour qu'on puisse enrichir
            ALIAS en une ligne.
    """
    chemin = Path(chemin)
    lecteur = LECTEURS.get(chemin.suffix.lower())
    if lecteur is None:
        raise ValueError(
            f"Format non pris en charge : {chemin.suffix}. "
            f"Attendus : {', '.join(sorted(LECTEURS))}"
        )

    immeubles: list[Immeuble] = []
    corr: dict[str, str] = {}
    colonnes_vues: list[str] = []
    lues = 0
    sans_identite = 0

    for ligne, geometrie in lecteur(chemin):
        if not corr:
            colonnes_vues = list(ligne.keys())
            corr = construire_correspondance(colonnes_vues)
            manquants = [c for c in CHAMPS_ESSENTIELS if c not in corr]
            if manquants:
                raise SchemaInconnu(
                    f"{chemin.name} : champs essentiels introuvables {manquants}.\n"
                    f"Colonnes présentes : {', '.join(colonnes_vues[:40])}\n"
                    f"→ ajouter les alias manquants dans core/foncier/sources/roles.py:ALIAS"
                )
            log.info(
                "foncier.roles: %s — %d/%d champs mappés",
                chemin.name, len(corr), len(ALIAS),
            )

        immeuble = ligne_vers_immeuble(ligne, corr, millesime, geometrie)
        if immeuble is None:
            sans_identite += 1
        else:
            immeubles.append(immeuble)

        lues += 1
        if limite and lues >= limite:
            break

    if sans_identite:
        log.warning(
            "foncier.roles: %d ligne(s) sans lot, matricule ni adresse — ignorées",
            sans_identite,
        )

    _verifier_valeurs(immeubles, chemin.name)
    log.info("foncier.roles: %d immeuble(s) chargé(s) depuis %s", len(immeubles), chemin.name)
    return immeubles


def _verifier_valeurs(immeubles: list[Immeuble], nom_source: str) -> None:
    """
    Alerte si la source ne porte pas de valeurs au rôle.

    Plusieurs extraits municipaux publient l'inventaire sans les valeurs. Le
    filtre de prix éliminerait alors 100 % des immeubles et on conclurait à tort
    que le marché est vide. Mieux vaut le dire fort.
    """
    if not immeubles:
        return
    sans_valeur = sum(1 for i in immeubles if not i.valeur_totale)
    part = sans_valeur / len(immeubles)
    if part > 0.9:
        log.warning(
            "foncier.roles: %s — %.0f %% des immeubles sans valeur au rôle. "
            "Le filtre de prix ne peut pas s'appliquer : utiliser une source "
            "avec valeurs, ou relancer avec le filtre de prix désactivé.",
            nom_source, part * 100,
        )


def charger_url(
    url: str,
    millesime: Optional[int] = None,
    limite: Optional[int] = None,
    forcer: bool = False,
) -> list[Immeuble]:
    """Télécharge (avec cache annuel) puis charge un rôle publié en ligne."""
    chemin = telecharger(url, ttl=TTL_ANNUEL, forcer=forcer)

    # Le cache stocke sans extension : on la restitue depuis l'URL pour choisir
    # le bon lecteur.
    suffixe = Path(url.split("?")[0]).suffix.lower()
    if suffixe in LECTEURS and chemin.suffix.lower() != suffixe:
        avec_suffixe = chemin.with_suffix(suffixe)
        if not avec_suffixe.exists():
            avec_suffixe.write_bytes(chemin.read_bytes())
        chemin = avec_suffixe

    return charger_fichier(chemin, millesime=millesime, limite=limite)


def distribution_cubf(immeubles: list[Immeuble], limite: int = 30) -> str:
    """
    Distribution réelle des codes d'utilisation dans un rôle chargé.

    Sert à valider les hypothèses de `criteres.py` avant de lancer une campagne
    sur une nouvelle municipalité :
        python dispatch.py foncier cubf --fichier data/role.csv
    """
    compteur = Counter(
        (i.cubf, i.libelle_utilisation or "") for i in immeubles if i.cubf is not None
    )
    total = sum(compteur.values())
    if not total:
        return "Aucun code d'utilisation lisible dans la source."

    lignes = [f"Distribution CUBF — {total} immeuble(s), {len(compteur)} code(s) distinct(s)"]
    for (code, libelle), n in compteur.most_common(limite):
        groupe = str(code)[0]
        lignes.append(f"  {code} (groupe {groupe}) — {n:>7} — {libelle[:45]}")
    return "\n".join(lignes)


def index_par_identifiant(immeubles: list[Immeuble]) -> dict[str, Immeuble]:
    """Index de déduplication, pour croiser deux millésimes ou deux sources."""
    index: dict[str, Immeuble] = {}
    for immeuble in immeubles:
        try:
            index[immeuble.identifiant] = immeuble
        except ValueError:
            continue
    return index


def comparer_millesimes(
    ancien: list[Immeuble], nouveau: list[Immeuble], seuil_variation: float = 0.15
) -> dict[str, list[Immeuble]]:
    """
    Compare deux millésimes du même rôle.

    C'est là qu'est la valeur de l'historique 2008-2026 : un immeuble dont la
    valeur bondit, dont le nombre de logements change ou qui apparaît/disparaît
    signale une transaction, une subdivision ou un redéveloppement.

    Returns:
        {"apparus": [...], "disparus": [...], "revalorises": [...], "restructures": [...]}
    """
    index_ancien = index_par_identifiant(ancien)
    index_nouveau = index_par_identifiant(nouveau)

    apparus = [i for cle, i in index_nouveau.items() if cle not in index_ancien]
    disparus = [i for cle, i in index_ancien.items() if cle not in index_nouveau]
    revalorises: list[Immeuble] = []
    restructures: list[Immeuble] = []

    for cle, recent in index_nouveau.items():
        precedent = index_ancien.get(cle)
        if precedent is None:
            continue

        if precedent.valeur_totale and recent.valeur_totale:
            variation = (recent.valeur_totale - precedent.valeur_totale) / precedent.valeur_totale
            if abs(variation) >= seuil_variation:
                revalorises.append(recent)

        if (
            precedent.nombre_logements is not None
            and recent.nombre_logements is not None
            and precedent.nombre_logements != recent.nombre_logements
        ) or (precedent.cubf != recent.cubf):
            restructures.append(recent)

    log.info(
        "foncier.roles: delta millésimes — %d apparus, %d disparus, %d revalorisés, %d restructurés",
        len(apparus), len(disparus), len(revalorises), len(restructures),
    )
    return {
        "apparus": apparus,
        "disparus": disparus,
        "revalorises": revalorises,
        "restructures": restructures,
    }
