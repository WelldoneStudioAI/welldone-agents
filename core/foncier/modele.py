"""
core/foncier/modele.py — Modèle de données normalisé.

Toutes les sources (rôles municipaux, SEAO, ventes pour taxes, JLR) convergent
vers `Immeuble`. Le scoring, le stockage et les briefs ne connaissent que ça.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Optional


@dataclass
class Signal:
    """
    Un fait daté et sourcé rattaché à un immeuble.

    Un signal n'est jamais une opinion : il porte toujours sa source et sa date,
    pour qu'un dossier reste auditable six mois plus tard.
    """

    cle: str                        # doit exister dans criteres.POIDS_PAR_CLE
    libelle: str                    # phrase lisible pour le brief
    source: str                     # "role_2026", "seao", "mrc_joliette", "jlr"
    date_signal: Optional[str] = None   # ISO 8601
    url: Optional[str] = None
    intensite: float = 1.0          # 0.0 à 1.0 — module le poids du signal
    donnees: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Immeuble:
    """Un candidat à l'acquisition, normalisé."""

    # ── Identité ────────────────────────────────────────────────────────────
    lot: Optional[str] = None            # numéro de lot cadastral
    matricule: Optional[str] = None      # matricule au rôle
    adresse: str = ""
    municipalite: str = ""
    arrondissement: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # ── Caractéristiques physiques (rôle d'évaluation) ──────────────────────
    cubf: Optional[int] = None
    libelle_utilisation: Optional[str] = None
    nombre_logements: Optional[int] = None
    nombre_etages: Optional[int] = None
    annee_construction: Optional[int] = None
    superficie_terrain: Optional[float] = None    # m²
    superficie_batiment: Optional[float] = None   # m²

    # ── Valeurs ─────────────────────────────────────────────────────────────
    valeur_terrain: Optional[float] = None
    valeur_batiment: Optional[float] = None
    valeur_totale: Optional[float] = None
    millesime: Optional[int] = None               # année du rôle

    # ── Enrichissement payant (registre foncier / JLR / fonciq) ─────────────
    proprietaire: Optional[str] = None
    date_acquisition: Optional[str] = None        # ISO 8601
    prix_acquisition: Optional[float] = None
    hypotheques: list[dict] = field(default_factory=list)
    enrichi_le: Optional[str] = None
    cout_enrichissement: float = 0.0              # $ dépensés sur ce dossier

    # ── Analyse ─────────────────────────────────────────────────────────────
    signaux: list[Signal] = field(default_factory=list)
    score: int = 0
    detail_score: dict[str, float] = field(default_factory=dict)
    noi_estime: Optional[float] = None
    cap_rate_estime: Optional[float] = None
    confiance_financiere: str = "aucune"          # aucune | faible | moyenne | forte

    # ── Suivi ───────────────────────────────────────────────────────────────
    statut: str = "nouveau"   # nouveau | surveille | enrichi | contacte | rejete
    note: Optional[str] = None
    vu_le: Optional[str] = None

    # ── Identité stable ─────────────────────────────────────────────────────

    @property
    def identifiant(self) -> str:
        """
        Clé de déduplication stable entre sources et entre millésimes.

        Le lot est la seule identité qui survit à un changement d'adresse ou de
        matricule ; on retombe sur le matricule, puis sur adresse+municipalité.

        L'adresse passe par `adresse.cle()` : sans normalisation, « 1450 ONTARIO E »
        du rôle et « 1450, rue Ontario Est » de Centris seraient deux immeubles.
        """
        from core.foncier.adresse import cle as cle_adresse

        for base in (
            f"lot:{self.lot}" if self.lot else None,
            f"mat:{self.municipalite}:{self.matricule}" if self.matricule else None,
            f"adr:{cle_adresse(self.adresse, self.municipalite)}" if self.adresse else None,
        ):
            if base:
                return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
        raise ValueError("Immeuble sans identité : ni lot, ni matricule, ni adresse")

    # ── Dérivés ─────────────────────────────────────────────────────────────

    @property
    def annees_detention(self) -> Optional[int]:
        """Nombre d'années depuis l'acquisition, si connue."""
        if not self.date_acquisition:
            return None
        try:
            acquise = datetime.fromisoformat(self.date_acquisition[:10]).date()
        except ValueError:
            return None
        return (date.today() - acquise).days // 365

    @property
    def ratio_occupation_sol(self) -> Optional[float]:
        """
        Superficie bâtie / superficie du terrain.

        Un ratio bas dans un secteur dense = du constructible non construit.
        C'est la mesure de sous-utilisation la plus directe qu'on puisse tirer
        du rôle seul.
        """
        if not self.superficie_terrain or not self.superficie_batiment:
            return None
        if self.superficie_terrain <= 0:
            return None
        return self.superficie_batiment / self.superficie_terrain

    @property
    def est_multiresidentiel(self) -> bool:
        from core.foncier.criteres import (
            GROUPE_RESIDENTIEL,
            LOGEMENTS_MIN_MULTIRESIDENTIEL,
            groupe_cubf,
        )

        return (
            groupe_cubf(self.cubf) == GROUPE_RESIDENTIEL
            and (self.nombre_logements or 0) >= LOGEMENTS_MIN_MULTIRESIDENTIEL
        )

    @property
    def est_commercial(self) -> bool:
        from core.foncier.criteres import GROUPES_COMMERCIAL, groupe_cubf

        return groupe_cubf(self.cubf) in GROUPES_COMMERCIAL

    @property
    def type_immeuble(self) -> str:
        """Catégorie utilisée par le module financier pour choisir ses ratios."""
        if self.est_multiresidentiel and self.est_commercial:
            return "mixte"
        if self.est_multiresidentiel:
            return "multiresidentiel"
        if self.est_commercial:
            return "commercial"
        return "autre"

    def a_signal(self, cle: str) -> bool:
        return any(s.cle == cle for s in self.signaux)

    def ajouter_signal(self, signal: Signal) -> None:
        """Ajoute un signal en évitant les doublons exacts (même clé, même source)."""
        for existant in self.signaux:
            if existant.cle == signal.cle and existant.source == signal.source:
                # On garde le plus intense des deux.
                if signal.intensite > existant.intensite:
                    self.signaux.remove(existant)
                    break
                return
        self.signaux.append(signal)

    # ── Sérialisation ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identifiant"] = self.identifiant
        d["annees_detention"] = self.annees_detention
        d["type_immeuble"] = self.type_immeuble
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "Immeuble":
        connus = {f for f in cls.__dataclass_fields__}
        signaux = [Signal(**s) for s in d.get("signaux", []) or []]
        immeuble = cls(**{k: v for k, v in d.items() if k in connus and k != "signaux"})
        immeuble.signaux = signaux
        return immeuble

    def resume(self) -> str:
        """Ligne unique pour Telegram et les logs."""
        prix = f"{self.valeur_totale:,.0f} $".replace(",", " ") if self.valeur_totale else "valeur inconnue"
        cap = f" · cap {self.cap_rate_estime:.1%}" if self.cap_rate_estime else ""
        detention = f" · {self.annees_detention} ans" if self.annees_detention else ""
        return f"[{self.score}] {self.adresse}, {self.municipalite} — {prix}{cap}{detention}"
