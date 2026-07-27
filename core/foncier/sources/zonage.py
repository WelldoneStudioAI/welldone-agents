"""
core/foncier/sources/zonage.py — Aires TOD et zones PPU.

Les deux signaux de « potentiel de développement » de la thèse, et les deux plus
difficiles à obtenir proprement parce qu'ils sont dispersés :

  · Aires TOD — la CMM les délimite sur un rayon de 500 m à 1 km autour des
    points d'accès au réseau de transport collectif métropolitain structurant.
    Le PMAD engage la Communauté à y diriger 40 % des nouveaux ménages
    2011-2031 : la densification y est une politique publique inscrite, pas un
    pari sur l'avenir.

  · Zones PPU — un programme particulier d'urbanisme précède la modification du
    zonage. C'est la fenêtre où le potentiel existe mais n'est pas encore dans
    le prix. Chaque municipalité publie les siennes à sa façon.

Deux modes d'alimentation, parce qu'aucune source unique ne couvre le Québec :

  1. POLYGONES — un GeoJSON de délimitation officielle (le plus juste)
  2. POINTS + RAYON — une liste de points d'accès au transport, dont on dérive
     les aires par rayon, exactement comme la CMM les construit

Aucune coordonnée n'est codée en dur dans ce module : les stations changent, les
aires sont révisées, et une donnée inventée serait pire que pas de donnée. Les
sources se configurent par variables d'environnement.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from typing import Optional

from core.foncier.criteres import (
    TOD_RATIO_PERIPHERIE,
    TOD_RAYON_COEUR_M,
    TOD_RAYON_ETENDU_M,
)
from core.foncier.geo import IndexSpatial, distance_m
from core.foncier.modele import Immeuble, Signal
from core.foncier.sources._http import SourceIndisponible, TTL_ANNUEL, telecharger

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
# Renseigner au moins l'une des deux pour activer le signal TOD.
#   FONCIER_TOD_POLYGONES_URL  → GeoJSON des aires TOD (Observatoire du Grand
#                                Montréal, section « Données géoréférencées »)
#   FONCIER_TOD_POINTS_URL     → GeoJSON ou CSV des points d'accès au transport
#                                structurant (stations de métro, gares, SRB, REM)
# FONCIER_PPU_URLS : URLs de GeoJSON de zones PPU, séparées par des virgules.

TOD_POLYGONES_URL = os.environ.get("FONCIER_TOD_POLYGONES_URL", "")
TOD_POINTS_URL = os.environ.get("FONCIER_TOD_POINTS_URL", "")
PPU_URLS = [u.strip() for u in os.environ.get("FONCIER_PPU_URLS", "").split(",") if u.strip()]

TOD_POLYGONES_FICHIER = os.environ.get("FONCIER_TOD_POLYGONES_FICHIER", "")
TOD_POINTS_FICHIER = os.environ.get("FONCIER_TOD_POINTS_FICHIER", "")


class ZonageNonConfigure(RuntimeError):
    """Aucune source de zonage n'est configurée — le signal reste simplement absent."""


# ─────────────────────────────────────────────────────────────────────────────
# Chargement
# ─────────────────────────────────────────────────────────────────────────────


def _charger_geojson(url: str = "", fichier: str = "") -> Optional[dict]:
    """Charge un GeoJSON depuis une URL (avec cache) ou un fichier local."""
    if fichier:
        chemin = Path(fichier)
        if not chemin.exists():
            log.warning("foncier.zonage: fichier introuvable — %s", fichier)
            return None
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)

    if not url:
        return None

    try:
        chemin = telecharger(url, ttl=TTL_ANNUEL)
    except SourceIndisponible as e:
        log.warning("foncier.zonage: %s", e)
        return None

    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("foncier.zonage: %s illisible (%s)", url, e)
        return None


def _charger_points(url: str = "", fichier: str = "") -> list[tuple[float, float, str]]:
    """
    Charge des points d'accès au transport depuis un GeoJSON ou un CSV.

    CSV attendu : colonnes latitude/longitude (ou lat/lon) et un nom optionnel.
    """
    source = fichier or url
    if not source:
        return []

    if source.lower().endswith(".csv"):
        chemin = Path(fichier) if fichier else telecharger(url, ttl=TTL_ANNUEL)
        points: list[tuple[float, float, str]] = []
        with open(chemin, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for ligne in csv.DictReader(f):
                cles = {k.strip().lower(): v for k, v in ligne.items() if k}
                lat = cles.get("latitude") or cles.get("lat") or cles.get("y")
                lon = cles.get("longitude") or cles.get("lon") or cles.get("long") or cles.get("x")
                nom = cles.get("nom") or cles.get("name") or cles.get("station") or "point d'accès"
                try:
                    points.append((float(lat), float(lon), str(nom)))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        return points

    geojson = _charger_geojson(url=url, fichier=fichier)
    if not geojson:
        return []

    points = []
    for feature in geojson.get("features", []):
        geometrie = feature.get("geometry") or {}
        if geometrie.get("type") != "Point":
            continue
        coords = geometrie.get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feature.get("properties") or {}
        nom = str(props.get("nom") or props.get("NOM") or props.get("name") or "point d'accès")
        points.append((float(coords[1]), float(coords[0]), nom))

    return points


class Zonage:
    """
    Index des aires TOD et zones PPU, construit une fois puis interrogé en boucle.

    Usage :
        zonage = Zonage.charger()
        signal = zonage.signal_tod(45.53, -73.51)
    """

    def __init__(self) -> None:
        self.tod_polygones = IndexSpatial()
        self.ppu_polygones = IndexSpatial()
        self.tod_points: list[tuple[float, float, str]] = []
        self.sources_actives: list[str] = []

    @classmethod
    def charger(cls) -> "Zonage":
        zonage = cls()

        geojson_tod = _charger_geojson(TOD_POLYGONES_URL, TOD_POLYGONES_FICHIER)
        if geojson_tod:
            n = zonage.tod_polygones.charger_geojson(geojson_tod)
            zonage.sources_actives.append(f"aires TOD (polygones) — {n} zone(s)")

        zonage.tod_points = _charger_points(TOD_POINTS_URL, TOD_POINTS_FICHIER)
        if zonage.tod_points:
            zonage.sources_actives.append(
                f"points d'accès transport — {len(zonage.tod_points)} point(s)"
            )

        for url in PPU_URLS:
            geojson_ppu = _charger_geojson(url=url)
            if geojson_ppu:
                n = zonage.ppu_polygones.charger_geojson(geojson_ppu)
                zonage.sources_actives.append(f"zones PPU — {n} zone(s)")

        if not zonage.sources_actives:
            log.warning(
                "foncier.zonage: aucune source configurée — les signaux TOD et PPU "
                "resteront absents. Voir FONCIER_TOD_* et FONCIER_PPU_URLS."
            )
        else:
            log.info("foncier.zonage: %s", " ; ".join(zonage.sources_actives))

        return zonage

    @property
    def actif(self) -> bool:
        return bool(self.sources_actives)

    # ── Interrogation ───────────────────────────────────────────────────────

    def signal_tod(self, lat: float, lon: float) -> Optional[Signal]:
        """Retourne un signal TOD si le point tombe dans une aire, sinon None."""
        # 1. Polygones officiels — la source la plus juste.
        zones = self.tod_polygones.chercher(lat, lon)
        if zones:
            nom, props = zones[0]
            return Signal(
                cle="aire_tod",
                libelle=f"Dans l'aire TOD « {nom} »",
                source="cmm_aires_tod",
                intensite=1.0,
                donnees={"zone": nom, **{k: v for k, v in list(props.items())[:5]}},
            )

        # 2. Dérivation par rayon autour des points d'accès, comme le fait la CMM.
        if self.tod_points:
            plus_proche = min(
                ((distance_m(lat, lon, plat, plon), nom) for plat, plon, nom in self.tod_points),
                default=None,
            )
            if plus_proche:
                distance, nom = plus_proche
                if distance <= TOD_RAYON_COEUR_M:
                    return Signal(
                        cle="aire_tod",
                        libelle=f"À {distance:.0f} m de {nom} (cœur d'aire TOD)",
                        source="points_transport",
                        intensite=1.0,
                        donnees={"distance_m": round(distance), "station": nom},
                    )
                if distance <= TOD_RAYON_ETENDU_M:
                    return Signal(
                        cle="aire_tod",
                        libelle=f"À {distance:.0f} m de {nom} (aire TOD étendue)",
                        source="points_transport",
                        intensite=TOD_RATIO_PERIPHERIE,
                        donnees={"distance_m": round(distance), "station": nom},
                    )

        return None

    def signal_ppu(self, lat: float, lon: float) -> Optional[Signal]:
        """Retourne un signal PPU si le point tombe dans une zone, sinon None."""
        zones = self.ppu_polygones.chercher(lat, lon)
        if not zones:
            return None
        nom, props = zones[0]
        return Signal(
            cle="zone_ppu",
            libelle=f"Dans la zone PPU « {nom} »",
            source="ppu_municipal",
            intensite=1.0,
            donnees={"zone": nom, **{k: v for k, v in list(props.items())[:5]}},
        )

    def enrichir(self, immeubles: list[Immeuble]) -> dict[str, int]:
        """
        Applique les signaux TOD et PPU à une collection d'immeubles.

        Returns:
            {"tod": n, "ppu": n, "sans_coordonnees": n}
        """
        compteur = {"tod": 0, "ppu": 0, "sans_coordonnees": 0}

        if not self.actif:
            return compteur

        for immeuble in immeubles:
            if immeuble.latitude is None or immeuble.longitude is None:
                compteur["sans_coordonnees"] += 1
                continue

            signal_tod = self.signal_tod(immeuble.latitude, immeuble.longitude)
            if signal_tod:
                immeuble.ajouter_signal(signal_tod)
                compteur["tod"] += 1

            signal_ppu = self.signal_ppu(immeuble.latitude, immeuble.longitude)
            if signal_ppu:
                immeuble.ajouter_signal(signal_ppu)
                compteur["ppu"] += 1

        if compteur["sans_coordonnees"]:
            log.warning(
                "foncier.zonage: %d immeuble(s) sans coordonnées — signaux TOD/PPU "
                "impossibles à évaluer. Utiliser une source géoréférencée du rôle.",
                compteur["sans_coordonnees"],
            )

        log.info(
            "foncier.zonage: %d immeuble(s) en aire TOD, %d en zone PPU",
            compteur["tod"], compteur["ppu"],
        )
        return compteur

    def diagnostic(self) -> str:
        """État des sources de zonage, pour `dispatch.py foncier sources`."""
        if not self.sources_actives:
            return (
                "Zonage NON CONFIGURÉ — les signaux « aire TOD » (20 pts) et "
                "« zone PPU » (15 pts) ne peuvent pas être attribués, soit 35 % du "
                "score total.\n"
                "  → FONCIER_TOD_POLYGONES_URL : GeoJSON des aires TOD de la CMM\n"
                "  → FONCIER_TOD_POINTS_URL    : points d'accès au transport structurant\n"
                "  → FONCIER_PPU_URLS          : GeoJSON des zones PPU municipales"
            )
        return "Zonage actif — " + " ; ".join(self.sources_actives)
