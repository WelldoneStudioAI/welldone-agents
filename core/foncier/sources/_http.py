"""
core/foncier/sources/_http.py — Téléchargement avec cache disque.

Les jeux de données fonciers sont gros (le rôle géoréférencé 2026 pèse 575 Mo) et
changent rarement — annuellement pour les rôles, hebdomadairement pour le SEAO.
Retélécharger à chaque exécution est un gaspillage de bande passante et une
impolitesse envers des serveurs publics.

Le cache respecte donc une durée de vie par type de source, et le module s'en
tient à `urllib` pour rester aligné sur le reste du repo (aucune dépendance
ajoutée).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("FONCIER_CACHE_DIR", "./data/foncier/cache"))
USER_AGENT = os.environ.get(
    "FONCIER_USER_AGENT",
    "welldone-agents/foncier (prospection immobilière; contact: awelldonestudio@gmail.com)",
)
TIMEOUT = int(os.environ.get("FONCIER_HTTP_TIMEOUT", "120"))

# Durées de vie par défaut, en secondes.
TTL_ANNUEL = 30 * 24 * 3600
TTL_HEBDO = 6 * 24 * 3600
TTL_QUOTIDIEN = 20 * 3600


class SourceIndisponible(RuntimeError):
    """Une source publique n'a pas répondu. Jamais silencieux, jamais fatal."""


def _chemin_cache(url: str, suffixe: str = "") -> Path:
    empreinte = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{empreinte}{suffixe}"


def telecharger(url: str, ttl: int = TTL_HEBDO, forcer: bool = False) -> Path:
    """
    Télécharge une URL vers le cache et retourne le chemin local.

    Args:
        url: l'adresse à récupérer
        ttl: durée de validité du cache en secondes
        forcer: ignore le cache et retélécharge

    Raises:
        SourceIndisponible: si le serveur ne répond pas et qu'aucun cache
            utilisable n'existe.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = _chemin_cache(url)

    if destination.exists() and not forcer:
        age = time.time() - destination.stat().st_mtime
        if age < ttl:
            log.debug("foncier.http: cache valide (%.0f h) pour %s", age / 3600, url)
            return destination

    requete = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporaire = destination.with_suffix(".partiel")

    try:
        with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:
            with open(temporaire, "wb") as sortie:
                shutil.copyfileobj(reponse, sortie)
        temporaire.replace(destination)
        log.info("foncier.http: téléchargé %s (%.1f Mo)", url, destination.stat().st_size / 1e6)
        return destination

    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
        temporaire.unlink(missing_ok=True)
        # Un cache périmé vaut mieux qu'une passe vide : on le signale et on
        # continue avec les données de la dernière fois.
        if destination.exists():
            age_h = (time.time() - destination.stat().st_mtime) / 3600
            log.warning(
                "foncier.http: %s injoignable (%s) — cache périmé de %.0f h utilisé",
                url, e, age_h,
            )
            return destination
        raise SourceIndisponible(f"{url} injoignable et aucun cache disponible : {e}") from e


def charger_json(url: str, ttl: int = TTL_HEBDO, forcer: bool = False) -> Any:
    """Télécharge et décode un JSON, en gérant la compression gzip transparente."""
    chemin = telecharger(url, ttl=ttl, forcer=forcer)
    return _decoder_json(chemin)


def _decoder_json(chemin: Path) -> Any:
    with open(chemin, "rb") as f:
        entete = f.read(2)
    ouvrir = gzip.open if entete == b"\x1f\x8b" else open
    with ouvrir(chemin, "rt", encoding="utf-8", errors="replace") as f:  # type: ignore[operator]
        return json.load(f)


def extraire_zip(chemin_zip: Path, motif: str | None = None) -> list[Path]:
    """
    Décompresse une archive dans le cache et retourne les fichiers extraits.

    Args:
        motif: si fourni, ne retourne que les fichiers dont le nom le contient
            (ex. ".geojson", ".csv")
    """
    destination = chemin_zip.with_suffix(".extrait")
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(chemin_zip) as archive:
        archive.extractall(destination)

    fichiers = sorted(p for p in destination.rglob("*") if p.is_file())
    if motif:
        fichiers = [p for p in fichiers if motif.lower() in p.name.lower()]
    return fichiers


def vider_cache(plus_vieux_que_jours: Optional[int] = None) -> int:
    """
    Supprime les fichiers du cache. Retourne le nombre de fichiers supprimés.

    Sans argument, vide tout. Utile quand un millésime de rôle est publié.
    """
    if not CACHE_DIR.exists():
        return 0

    supprimes = 0
    limite = time.time() - (plus_vieux_que_jours or 0) * 86400

    for chemin in CACHE_DIR.iterdir():
        if plus_vieux_que_jours is not None and chemin.stat().st_mtime > limite:
            continue
        if chemin.is_dir():
            shutil.rmtree(chemin, ignore_errors=True)
        else:
            chemin.unlink(missing_ok=True)
        supprimes += 1

    return supprimes
