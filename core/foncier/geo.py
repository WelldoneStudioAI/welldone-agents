"""
core/foncier/geo.py — Géométrie minimale, sans dépendance.

Le moteur a besoin de deux choses : « ce point est-il dans ce polygone ? »
(aires TOD, zones PPU) et « quelle distance entre ces deux points ? » (rayon
autour d'un avis SEAO). Shapely ferait le travail mais ajoute une dépendance
compilée pour deux fonctions de vingt lignes.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

RAYON_TERRE_M = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en mètres entre deux points (formule de haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * RAYON_TERRE_M * math.asin(math.sqrt(a))


def _point_dans_anneau(lon: float, lat: float, anneau: list[list[float]]) -> bool:
    """
    Test d'inclusion par lancer de rayon (ray casting).

    Les coordonnées GeoJSON sont en [longitude, latitude] — dans cet ordre.
    """
    dedans = False
    n = len(anneau)
    j = n - 1
    for i in range(n):
        xi, yi = anneau[i][0], anneau[i][1]
        xj, yj = anneau[j][0], anneau[j][1]
        # Le segment croise-t-il l'horizontale passant par le point ?
        if (yi > lat) != (yj > lat):
            # Abscisse de l'intersection.
            x_intersection = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_intersection:
                dedans = not dedans
        j = i
    return dedans


def _point_dans_polygone(lon: float, lat: float, coords: list) -> bool:
    """
    Un polygone GeoJSON = [anneau_extérieur, trou1, trou2, ...].
    Le point compte s'il est dans l'extérieur ET dans aucun trou.
    """
    if not coords:
        return False
    if not _point_dans_anneau(lon, lat, coords[0]):
        return False
    for trou in coords[1:]:
        if _point_dans_anneau(lon, lat, trou):
            return False
    return True


def point_dans_geometrie(lat: float, lon: float, geometrie: dict) -> bool:
    """Teste un point contre une géométrie GeoJSON Polygon ou MultiPolygon."""
    if not geometrie:
        return False
    type_geo = geometrie.get("type")
    coords = geometrie.get("coordinates") or []

    if type_geo == "Polygon":
        return _point_dans_polygone(lon, lat, coords)
    if type_geo == "MultiPolygon":
        return any(_point_dans_polygone(lon, lat, poly) for poly in coords)
    if type_geo == "GeometryCollection":
        return any(
            point_dans_geometrie(lat, lon, g) for g in geometrie.get("geometries", [])
        )
    return False


def bbox_geometrie(geometrie: dict) -> Optional[tuple[float, float, float, float]]:
    """
    Boîte englobante (lon_min, lat_min, lon_max, lat_max).

    Sert de pré-filtre : comparer 4 flottants avant de lancer le ray casting fait
    la différence entre un scan de quelques secondes et un scan de plusieurs
    minutes sur 500 000 unités d'évaluation.
    """
    lons: list[float] = []
    lats: list[float] = []

    def parcourir(noeud: Any) -> None:
        if isinstance(noeud, (list, tuple)):
            if (
                len(noeud) >= 2
                and isinstance(noeud[0], (int, float))
                and isinstance(noeud[1], (int, float))
            ):
                lons.append(float(noeud[0]))
                lats.append(float(noeud[1]))
            else:
                for enfant in noeud:
                    parcourir(enfant)

    parcourir(geometrie.get("coordinates") or [])
    if not lons or not lats:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def centroide(geometrie: dict) -> Optional[tuple[float, float]]:
    """Centre de la boîte englobante — approximation suffisante pour un rayon."""
    bbox = bbox_geometrie(geometrie)
    if not bbox:
        return None
    lon_min, lat_min, lon_max, lat_max = bbox
    return ((lat_min + lat_max) / 2, (lon_min + lon_max) / 2)


class IndexSpatial:
    """
    Index de polygones avec pré-filtre par boîte englobante.

    Usage :
        index = IndexSpatial()
        index.ajouter("Aire TOD Longueuil", geometrie, {"rayon": 500})
        for nom, props in index.chercher(45.53, -73.51):
            ...
    """

    def __init__(self) -> None:
        self._entrees: list[tuple[str, dict, dict, tuple[float, float, float, float]]] = []

    def ajouter(self, nom: str, geometrie: dict, proprietes: dict | None = None) -> bool:
        bbox = bbox_geometrie(geometrie)
        if bbox is None:
            return False
        self._entrees.append((nom, geometrie, proprietes or {}, bbox))
        return True

    def charger_geojson(self, geojson: dict, cle_nom: str = "nom") -> int:
        """Charge une FeatureCollection. Retourne le nombre de polygones indexés."""
        n = 0
        for feature in geojson.get("features", []):
            props = feature.get("properties") or {}
            nom = str(props.get(cle_nom) or props.get("NOM") or props.get("name") or f"zone_{n}")
            if self.ajouter(nom, feature.get("geometry") or {}, props):
                n += 1
        return n

    def chercher(self, lat: float, lon: float) -> list[tuple[str, dict]]:
        """Retourne toutes les zones contenant le point."""
        resultats = []
        for nom, geometrie, props, (lon_min, lat_min, lon_max, lat_max) in self._entrees:
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                continue
            if point_dans_geometrie(lat, lon, geometrie):
                resultats.append((nom, props))
        return resultats

    def __len__(self) -> int:
        return len(self._entrees)


def plus_proche(
    lat: float, lon: float, points: Iterable[tuple[float, float, Any]]
) -> Optional[tuple[float, Any]]:
    """
    Parmi une collection de (lat, lon, charge_utile), retourne (distance_m, charge)
    du plus proche. None si la collection est vide.
    """
    meilleur: Optional[tuple[float, Any]] = None
    for plat, plon, charge in points:
        d = distance_m(lat, lon, plat, plon)
        if meilleur is None or d < meilleur[0]:
            meilleur = (d, charge)
    return meilleur
