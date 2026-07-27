"""
core/foncier/store.py — Persistance des candidats.

SQLite, volontairement. Le reste du repo garde son état dans Notion ou en
mémoire, mais la prospection foncière a deux besoins que ça ne couvre pas :

  · l'HISTORIQUE — savoir qu'un immeuble a été vu il y a huit mois à 41 points
    et qu'il est monté à 72 est le signal, pas le score du jour ;
  · la MÉMOIRE DES DÉPENSES — ne jamais payer deux fois pour le même dossier.

⚠️ Sur Railway, le système de fichiers est éphémère : sans volume monté, la base
   est perdue à chaque redéploiement. Monter un volume et pointer
   FONCIER_DB_PATH dessus, ou accepter de repartir de zéro à chaque déploiement.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from core.foncier.modele import Immeuble

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("FONCIER_DB_PATH", "./data/foncier/foncier.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS immeubles (
    identifiant       TEXT PRIMARY KEY,
    lot               TEXT,
    matricule         TEXT,
    adresse           TEXT,
    municipalite      TEXT,
    cubf              INTEGER,
    nombre_logements  INTEGER,
    valeur_totale     REAL,
    latitude          REAL,
    longitude         REAL,
    score             INTEGER DEFAULT 0,
    cap_rate_estime   REAL,
    noi_estime        REAL,
    proprietaire      TEXT,
    date_acquisition  TEXT,
    statut            TEXT DEFAULT 'nouveau',
    note              TEXT,
    cout_cumule       REAL DEFAULT 0,
    donnees           TEXT NOT NULL,
    vu_le             TEXT,
    maj_le            TEXT
);

CREATE INDEX IF NOT EXISTS idx_score ON immeubles(score DESC);
CREATE INDEX IF NOT EXISTS idx_municipalite ON immeubles(municipalite);
CREATE INDEX IF NOT EXISTS idx_statut ON immeubles(statut);

-- Historique des scores : c'est la variation qui porte l'information.
CREATE TABLE IF NOT EXISTS historique_scores (
    identifiant  TEXT NOT NULL,
    date_mesure  TEXT NOT NULL,
    score        INTEGER NOT NULL,
    detail       TEXT,
    PRIMARY KEY (identifiant, date_mesure)
);

-- Journal des dépenses d'enrichissement, pour ne jamais payer deux fois.
CREATE TABLE IF NOT EXISTS depenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant  TEXT NOT NULL,
    fournisseur  TEXT,
    cout         REAL NOT NULL,
    date_appel   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_depenses_date ON depenses(date_appel);
"""


def connexion(chemin: Optional[Path] = None) -> sqlite3.Connection:
    """Ouvre la base et applique le schéma si nécessaire."""
    chemin = chemin or DB_PATH
    chemin.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Écriture
# ─────────────────────────────────────────────────────────────────────────────


def enregistrer(immeubles: Iterable[Immeuble], conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Insère ou met à jour des immeubles, en fusionnant avec ce qui est déjà connu.

    La fusion préserve ce qui a coûté de l'argent : un enrichissement payé ne
    doit jamais être écrasé par une passe gratuite qui ne connaît pas le
    propriétaire.

    Returns:
        {"nouveaux": n, "mis_a_jour": n, "progressions": [(identifiant, ancien, nouveau)]}
    """
    fermer = conn is None
    conn = conn or connexion()
    aujourdhui = date.today().isoformat()
    maintenant = datetime.now().isoformat(timespec="seconds")

    nouveaux = 0
    mis_a_jour = 0
    progressions: list[tuple[str, int, int]] = []

    try:
        for immeuble in immeubles:
            try:
                identifiant = immeuble.identifiant
            except ValueError:
                continue

            ligne = conn.execute(
                "SELECT score, donnees FROM immeubles WHERE identifiant = ?", (identifiant,)
            ).fetchone()

            if ligne is not None:
                ancien = Immeuble.from_dict(json.loads(ligne["donnees"]))
                immeuble = _fusionner(ancien, immeuble)
                ancien_score = ligne["score"] or 0
                if immeuble.score > ancien_score:
                    progressions.append((identifiant, ancien_score, immeuble.score))
                mis_a_jour += 1
            else:
                nouveaux += 1
                immeuble.vu_le = immeuble.vu_le or aujourdhui

            conn.execute(
                """
                INSERT INTO immeubles (
                    identifiant, lot, matricule, adresse, municipalite, cubf,
                    nombre_logements, valeur_totale, latitude, longitude, score,
                    cap_rate_estime, noi_estime, proprietaire, date_acquisition,
                    statut, note, cout_cumule, donnees, vu_le, maj_le
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(identifiant) DO UPDATE SET
                    lot=excluded.lot, matricule=excluded.matricule,
                    adresse=excluded.adresse, municipalite=excluded.municipalite,
                    cubf=excluded.cubf, nombre_logements=excluded.nombre_logements,
                    valeur_totale=excluded.valeur_totale,
                    latitude=excluded.latitude, longitude=excluded.longitude,
                    score=excluded.score, cap_rate_estime=excluded.cap_rate_estime,
                    noi_estime=excluded.noi_estime, proprietaire=excluded.proprietaire,
                    date_acquisition=excluded.date_acquisition,
                    statut=excluded.statut, note=excluded.note,
                    cout_cumule=excluded.cout_cumule, donnees=excluded.donnees,
                    maj_le=excluded.maj_le
                """,
                (
                    identifiant, immeuble.lot, immeuble.matricule, immeuble.adresse,
                    immeuble.municipalite, immeuble.cubf, immeuble.nombre_logements,
                    immeuble.valeur_totale, immeuble.latitude, immeuble.longitude,
                    immeuble.score, immeuble.cap_rate_estime, immeuble.noi_estime,
                    immeuble.proprietaire, immeuble.date_acquisition, immeuble.statut,
                    immeuble.note, immeuble.cout_enrichissement, immeuble.to_json(),
                    immeuble.vu_le or aujourdhui, maintenant,
                ),
            )

            conn.execute(
                """
                INSERT INTO historique_scores (identifiant, date_mesure, score, detail)
                VALUES (?,?,?,?)
                ON CONFLICT(identifiant, date_mesure) DO UPDATE SET
                    score=excluded.score, detail=excluded.detail
                """,
                (identifiant, aujourdhui, immeuble.score, json.dumps(immeuble.detail_score)),
            )

            if immeuble.cout_enrichissement > 0 and immeuble.enrichi_le == aujourdhui:
                deja = conn.execute(
                    "SELECT 1 FROM depenses WHERE identifiant = ? AND date_appel = ?",
                    (identifiant, aujourdhui),
                ).fetchone()
                if not deja:
                    conn.execute(
                        "INSERT INTO depenses (identifiant, fournisseur, cout, date_appel) "
                        "VALUES (?,?,?,?)",
                        (identifiant, "registre", immeuble.cout_enrichissement, aujourdhui),
                    )

        conn.commit()
    finally:
        if fermer:
            conn.close()

    log.info(
        "foncier.store: %d nouveau(x), %d mis à jour, %d progression(s) de score",
        nouveaux, mis_a_jour, len(progressions),
    )
    return {"nouveaux": nouveaux, "mis_a_jour": mis_a_jour, "progressions": progressions}


def _fusionner(ancien: Immeuble, nouveau: Immeuble) -> Immeuble:
    """
    Fusionne deux versions du même immeuble.

    Le nouveau gagne sur les données du rôle (elles sont plus fraîches).
    L'ancien gagne sur tout ce qui a été payé ou saisi à la main.
    """
    # Données payées : jamais écrasées par du vide.
    nouveau.proprietaire = nouveau.proprietaire or ancien.proprietaire
    nouveau.date_acquisition = nouveau.date_acquisition or ancien.date_acquisition
    nouveau.prix_acquisition = nouveau.prix_acquisition or ancien.prix_acquisition
    nouveau.hypotheques = nouveau.hypotheques or ancien.hypotheques
    nouveau.enrichi_le = nouveau.enrichi_le or ancien.enrichi_le
    nouveau.cout_enrichissement = max(nouveau.cout_enrichissement, ancien.cout_enrichissement)

    # Saisie humaine : toujours préservée.
    nouveau.note = nouveau.note or ancien.note
    if ancien.statut in ("contacte", "rejete") and nouveau.statut == "nouveau":
        nouveau.statut = ancien.statut

    nouveau.vu_le = ancien.vu_le or nouveau.vu_le

    # Les signaux s'accumulent : un préavis vu en mars reste vrai en juin.
    for signal in ancien.signaux:
        nouveau.ajouter_signal(signal)

    return nouveau


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────


def charger(identifiant: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Immeuble]:
    fermer = conn is None
    conn = conn or connexion()
    try:
        ligne = conn.execute(
            "SELECT donnees FROM immeubles WHERE identifiant = ?", (identifiant,)
        ).fetchone()
        return Immeuble.from_dict(json.loads(ligne["donnees"])) if ligne else None
    finally:
        if fermer:
            conn.close()


def meilleurs(
    limite: int = 20,
    score_minimum: int = 0,
    statuts: Optional[tuple[str, ...]] = None,
    municipalite: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[Immeuble]:
    """Les meilleurs dossiers en base, filtrés."""
    fermer = conn is None
    conn = conn or connexion()

    requete = "SELECT donnees FROM immeubles WHERE score >= ?"
    parametres: list = [score_minimum]

    if statuts:
        requete += f" AND statut IN ({','.join('?' * len(statuts))})"
        parametres.extend(statuts)
    if municipalite:
        requete += " AND LOWER(municipalite) LIKE ?"
        parametres.append(f"%{municipalite.lower()}%")

    requete += " ORDER BY score DESC, valeur_totale DESC LIMIT ?"
    parametres.append(limite)

    try:
        lignes = conn.execute(requete, parametres).fetchall()
        return [Immeuble.from_dict(json.loads(l["donnees"])) for l in lignes]
    finally:
        if fermer:
            conn.close()


def deja_enrichi(identifiant: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Vrai si on a déjà payé pour ce dossier — le garde-fou anti-double-dépense."""
    fermer = conn is None
    conn = conn or connexion()
    try:
        ligne = conn.execute(
            "SELECT 1 FROM depenses WHERE identifiant = ? LIMIT 1", (identifiant,)
        ).fetchone()
        return ligne is not None
    finally:
        if fermer:
            conn.close()


def depenses_du_mois(conn: Optional[sqlite3.Connection] = None) -> float:
    """Total dépensé en enrichissement depuis le début du mois courant."""
    fermer = conn is None
    conn = conn or connexion()
    debut = date.today().replace(day=1).isoformat()
    try:
        ligne = conn.execute(
            "SELECT COALESCE(SUM(cout), 0) AS total FROM depenses WHERE date_appel >= ?",
            (debut,),
        ).fetchone()
        return float(ligne["total"] or 0)
    finally:
        if fermer:
            conn.close()


def progressions(
    jours: int = 30, delta_minimum: int = 10, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    """
    Immeubles dont le score a progressé — le vrai signal du suivi longitudinal.

    Un immeuble qui passe de 40 à 70 vient d'accumuler des signaux : c'est plus
    actionnable qu'un immeuble stable à 70 depuis un an.
    """
    fermer = conn is None
    conn = conn or connexion()
    try:
        lignes = conn.execute(
            """
            SELECT h.identifiant,
                   MIN(h.score) AS score_min,
                   MAX(h.score) AS score_max,
                   i.adresse, i.municipalite, i.valeur_totale
            FROM historique_scores h
            JOIN immeubles i ON i.identifiant = h.identifiant
            WHERE h.date_mesure >= date('now', ?)
            GROUP BY h.identifiant
            HAVING (MAX(h.score) - MIN(h.score)) >= ?
            ORDER BY (MAX(h.score) - MIN(h.score)) DESC
            """,
            (f"-{jours} days", delta_minimum),
        ).fetchall()
        return [
            {
                "identifiant": l["identifiant"],
                "adresse": l["adresse"],
                "municipalite": l["municipalite"],
                "valeur_totale": l["valeur_totale"],
                "progression": l["score_max"] - l["score_min"],
                "score": l["score_max"],
            }
            for l in lignes
        ]
    finally:
        if fermer:
            conn.close()


def statistiques(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Portrait de la base, pour le rapport hebdomadaire."""
    fermer = conn is None
    conn = conn or connexion()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM immeubles").fetchone()["n"]
        par_statut = {
            l["statut"]: l["n"]
            for l in conn.execute(
                "SELECT statut, COUNT(*) AS n FROM immeubles GROUP BY statut"
            ).fetchall()
        }
        haut = conn.execute(
            "SELECT COUNT(*) AS n FROM immeubles WHERE score >= 60"
        ).fetchone()["n"]
        return {
            "total": total,
            "par_statut": par_statut,
            "score_60_et_plus": haut,
            "depenses_mois": depenses_du_mois(conn),
        }
    finally:
        if fermer:
            conn.close()


def marquer(
    identifiant: str,
    statut: Optional[str] = None,
    note: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Change le statut ou la note d'un dossier (suivi manuel de JP)."""
    fermer = conn is None
    conn = conn or connexion()
    try:
        immeuble = charger(identifiant, conn)
        if immeuble is None:
            return False
        if statut:
            immeuble.statut = statut
        if note:
            immeuble.note = note
        conn.execute(
            "UPDATE immeubles SET statut = ?, note = ?, donnees = ?, maj_le = ? "
            "WHERE identifiant = ?",
            (
                immeuble.statut,
                immeuble.note,
                immeuble.to_json(),
                datetime.now().isoformat(timespec="seconds"),
                identifiant,
            ),
        )
        conn.commit()
        return True
    finally:
        if fermer:
            conn.close()
