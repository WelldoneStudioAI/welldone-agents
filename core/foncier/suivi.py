"""
core/foncier/suivi.py — CRM de prospection foncière.

Le moteur trouve des immeubles. Ce module suit ce que tu en fais.

Trois choses qu'aucune autre partie du système ne porte :

  · QUI a été contacté, QUAND, et ce qui s'est dit
  · QUAND relancer — la prospection foncière se joue sur des mois, et le
    dossier qu'on oublie de relancer est le dossier qu'on perd
  · CE QUI BOUGE — un immeuble dont le score monte, qui arrive sur le marché
    ou sur lequel un préavis apparaît mérite qu'on le rappelle aujourd'hui,
    pas au prochain tri

Les tables vivent dans la même base SQLite que les dossiers : un dossier et son
historique de contacts ne doivent jamais pouvoir se désynchroniser.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from core.foncier import store

log = logging.getLogger(__name__)

SCHEMA_SUIVI = """
-- Personnes rattachées à un dossier : propriétaire, courtier, notaire, voisin.
CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant  TEXT NOT NULL,
    nom          TEXT NOT NULL,
    role         TEXT DEFAULT 'proprietaire',
    telephone    TEXT,
    courriel     TEXT,
    notes        TEXT,
    cree_le      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_dossier ON contacts(identifiant);

-- Journal des interactions. Append-only : on ne réécrit jamais l'histoire.
CREATE TABLE IF NOT EXISTS interactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant  TEXT NOT NULL,
    date_contact TEXT NOT NULL,
    canal        TEXT NOT NULL,
    resume       TEXT NOT NULL,
    resultat     TEXT,
    contact_id   INTEGER,
    cree_le      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interactions_dossier ON interactions(identifiant);
CREATE INDEX IF NOT EXISTS idx_interactions_date ON interactions(date_contact DESC);

-- Relances programmées.
CREATE TABLE IF NOT EXISTS relances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant  TEXT NOT NULL,
    date_prevue  TEXT NOT NULL,
    motif        TEXT NOT NULL,
    fait         INTEGER DEFAULT 0,
    fait_le      TEXT,
    cree_le      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relances_date ON relances(date_prevue, fait);
"""

CANAUX = ("telephone", "courriel", "poste", "porte", "rencontre", "courtier", "autre")

RESULTATS = (
    "sans_reponse",
    "a_rappeler",
    "refus",
    "interesse",
    "etats_financiers_demandes",
    "visite_planifiee",
    "offre_deposee",
    "vendu_a_un_autre",
)

# Délai de relance par défaut selon le résultat de la dernière interaction.
DELAI_RELANCE = {
    "sans_reponse": 14,
    "a_rappeler": 7,
    "interesse": 5,
    "etats_financiers_demandes": 10,
    "visite_planifiee": 3,
    "offre_deposee": 5,
    "refus": 180,          # un refus n'est pas éternel — six mois, puis on retente
    "vendu_a_un_autre": 0,  # dossier clos
}


def connexion(chemin=None) -> sqlite3.Connection:
    """Ouvre la base et garantit la présence des tables de suivi."""
    conn = store.connexion(chemin)
    conn.executescript(SCHEMA_SUIVI)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Contacts
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Contact:
    identifiant: str
    nom: str
    role: str = "proprietaire"
    telephone: Optional[str] = None
    courriel: Optional[str] = None
    notes: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "identifiant": self.identifiant,
            "nom": self.nom,
            "role": self.role,
            "telephone": self.telephone,
            "courriel": self.courriel,
            "notes": self.notes,
        }


def ajouter_contact(contact: Contact, conn: Optional[sqlite3.Connection] = None) -> int:
    """Rattache une personne à un dossier. Retourne son identifiant interne."""
    fermer = conn is None
    conn = conn or connexion()
    try:
        curseur = conn.execute(
            "INSERT INTO contacts (identifiant, nom, role, telephone, courriel, notes, cree_le) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                contact.identifiant, contact.nom, contact.role, contact.telephone,
                contact.courriel, contact.notes,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(curseur.lastrowid or 0)
    finally:
        if fermer:
            conn.close()


def contacts_du_dossier(
    identifiant: str, conn: Optional[sqlite3.Connection] = None
) -> list[Contact]:
    fermer = conn is None
    conn = conn or connexion()
    try:
        lignes = conn.execute(
            "SELECT * FROM contacts WHERE identifiant = ? ORDER BY id", (identifiant,)
        ).fetchall()
        return [
            Contact(
                id=l["id"], identifiant=l["identifiant"], nom=l["nom"], role=l["role"],
                telephone=l["telephone"], courriel=l["courriel"], notes=l["notes"],
            )
            for l in lignes
        ]
    finally:
        if fermer:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Journal des interactions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Interaction:
    identifiant: str
    canal: str
    resume: str
    resultat: Optional[str] = None
    date_contact: str = ""
    contact_id: Optional[int] = None
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "identifiant": self.identifiant,
            "date_contact": self.date_contact,
            "canal": self.canal,
            "resume": self.resume,
            "resultat": self.resultat,
            "contact_id": self.contact_id,
        }


def journaliser(
    interaction: Interaction,
    programmer_suite: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """
    Consigne une interaction et programme automatiquement la relance.

    C'est le point d'entrée principal du CRM : après chaque appel, une ligne, et
    la prochaine action se planifie toute seule selon le résultat. Un dossier
    « intéressé » sans relance programmée est un dossier perdu.

    Returns:
        {"interaction_id": n, "relance": date ISO ou None, "statut": statut appliqué}
    """
    fermer = conn is None
    conn = conn or connexion()

    if interaction.canal not in CANAUX:
        raise ValueError(f"Canal inconnu « {interaction.canal} ». Valides : {', '.join(CANAUX)}")
    if interaction.resultat and interaction.resultat not in RESULTATS:
        raise ValueError(
            f"Résultat inconnu « {interaction.resultat} ». Valides : {', '.join(RESULTATS)}"
        )

    aujourdhui = date.today().isoformat()
    interaction.date_contact = interaction.date_contact or aujourdhui

    try:
        curseur = conn.execute(
            "INSERT INTO interactions "
            "(identifiant, date_contact, canal, resume, resultat, contact_id, cree_le) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                interaction.identifiant, interaction.date_contact, interaction.canal,
                interaction.resume, interaction.resultat, interaction.contact_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        interaction_id = int(curseur.lastrowid or 0)

        # Le dossier passe automatiquement en « contacté » — le statut suit
        # l'action réelle plutôt que d'attendre une mise à jour manuelle.
        statut = "contacte"
        if interaction.resultat == "vendu_a_un_autre":
            statut = "rejete"
        elif interaction.resultat == "refus":
            statut = "surveille"
        store.marquer(interaction.identifiant, statut=statut, conn=conn)

        date_relance = None
        if programmer_suite and interaction.resultat:
            delai = DELAI_RELANCE.get(interaction.resultat, 14)
            if delai > 0:
                date_relance = (date.today() + timedelta(days=delai)).isoformat()
                conn.execute(
                    "INSERT INTO relances (identifiant, date_prevue, motif, cree_le) "
                    "VALUES (?,?,?,?)",
                    (
                        interaction.identifiant,
                        date_relance,
                        f"Suite à « {interaction.resultat} » du {interaction.date_contact}",
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )

        conn.commit()
        log.info(
            "foncier.suivi: interaction %s sur %s — relance %s",
            interaction.canal, interaction.identifiant, date_relance or "aucune",
        )
        return {
            "interaction_id": interaction_id,
            "relance": date_relance,
            "statut": statut,
        }
    finally:
        if fermer:
            conn.close()


def historique(
    identifiant: str, conn: Optional[sqlite3.Connection] = None
) -> list[Interaction]:
    """Toutes les interactions d'un dossier, de la plus récente à la plus ancienne."""
    fermer = conn is None
    conn = conn or connexion()
    try:
        lignes = conn.execute(
            "SELECT * FROM interactions WHERE identifiant = ? "
            "ORDER BY date_contact DESC, id DESC",
            (identifiant,),
        ).fetchall()
        return [
            Interaction(
                id=l["id"], identifiant=l["identifiant"], date_contact=l["date_contact"],
                canal=l["canal"], resume=l["resume"], resultat=l["resultat"],
                contact_id=l["contact_id"],
            )
            for l in lignes
        ]
    finally:
        if fermer:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Relances
# ─────────────────────────────────────────────────────────────────────────────


def programmer_relance(
    identifiant: str,
    dans_jours: int = 14,
    motif: str = "Relance",
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Programme une relance manuelle. Retourne la date prévue."""
    fermer = conn is None
    conn = conn or connexion()
    date_prevue = (date.today() + timedelta(days=max(0, dans_jours))).isoformat()
    try:
        conn.execute(
            "INSERT INTO relances (identifiant, date_prevue, motif, cree_le) VALUES (?,?,?,?)",
            (identifiant, date_prevue, motif, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return date_prevue
    finally:
        if fermer:
            conn.close()


def relances_dues(
    jusqu_a: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    """
    Relances à faire aujourd'hui ou en retard.

    C'est la liste sur laquelle commencer sa journée.
    """
    fermer = conn is None
    conn = conn or connexion()
    limite = jusqu_a or date.today().isoformat()
    try:
        lignes = conn.execute(
            """
            SELECT r.id, r.identifiant, r.date_prevue, r.motif,
                   i.adresse, i.municipalite, i.score, i.proprietaire, i.statut
            FROM relances r
            LEFT JOIN immeubles i ON i.identifiant = r.identifiant
            WHERE r.fait = 0 AND r.date_prevue <= ?
            ORDER BY r.date_prevue, i.score DESC
            """,
            (limite,),
        ).fetchall()
        return [
            {
                "relance_id": l["id"],
                "identifiant": l["identifiant"],
                "date_prevue": l["date_prevue"],
                "motif": l["motif"],
                "adresse": l["adresse"],
                "municipalite": l["municipalite"],
                "score": l["score"],
                "proprietaire": l["proprietaire"],
                "statut": l["statut"],
                "en_retard": l["date_prevue"] < date.today().isoformat(),
            }
            for l in lignes
        ]
    finally:
        if fermer:
            conn.close()


def marquer_relance_faite(
    relance_id: int, conn: Optional[sqlite3.Connection] = None
) -> bool:
    fermer = conn is None
    conn = conn or connexion()
    try:
        curseur = conn.execute(
            "UPDATE relances SET fait = 1, fait_le = ? WHERE id = ? AND fait = 0",
            (date.today().isoformat(), relance_id),
        )
        conn.commit()
        return curseur.rowcount > 0
    finally:
        if fermer:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui bouge
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Mouvement:
    """Un changement d'état digne d'une action immédiate."""

    identifiant: str
    adresse: str
    municipalite: str
    score: int
    genre: str          # progression | mise_en_marche | detresse | relance_due
    detail: str
    urgence: int = 0    # tri : plus haut = plus urgent

    def to_dict(self) -> dict:
        return {
            "identifiant": self.identifiant,
            "adresse": self.adresse,
            "municipalite": self.municipalite,
            "score": self.score,
            "genre": self.genre,
            "detail": self.detail,
            "urgence": self.urgence,
        }


def mouvements(
    jours: int = 14, conn: Optional[sqlite3.Connection] = None
) -> list[Mouvement]:
    """
    Ce qui a bougé récemment et mérite une action.

    Un dossier stable à 70 points depuis un an est moins actionnable qu'un
    dossier passé de 40 à 68 la semaine dernière : la variation porte
    l'information, pas le niveau.
    """
    fermer = conn is None
    conn = conn or connexion()
    resultats: list[Mouvement] = []

    try:
        # 1. Scores en progression.
        for progression in store.progressions(jours, 10, conn):
            resultats.append(
                Mouvement(
                    identifiant=progression["identifiant"],
                    adresse=progression["adresse"] or "",
                    municipalite=progression["municipalite"] or "",
                    score=progression["score"],
                    genre="progression",
                    detail=f"+{progression['progression']} points sur {jours} jours",
                    urgence=50 + progression["progression"],
                )
            )

        # 2. Mises en marché et signaux de détresse récents.
        lignes = conn.execute(
            "SELECT identifiant, adresse, municipalite, score, donnees "
            "FROM immeubles WHERE maj_le >= date('now', ?) ORDER BY score DESC LIMIT 200",
            (f"-{jours} days",),
        ).fetchall()

        limite = (date.today() - timedelta(days=jours)).isoformat()

        for ligne in lignes:
            try:
                donnees = json.loads(ligne["donnees"])
            except (json.JSONDecodeError, TypeError):
                continue

            for signal in donnees.get("signaux") or []:
                date_signal = (signal.get("date_signal") or "")[:10]
                if date_signal and date_signal < limite:
                    continue

                if signal.get("cle") == "mise_en_marche":
                    resultats.append(
                        Mouvement(
                            identifiant=ligne["identifiant"],
                            adresse=ligne["adresse"] or "",
                            municipalite=ligne["municipalite"] or "",
                            score=ligne["score"] or 0,
                            genre="mise_en_marche",
                            detail=signal.get("libelle") or "Mis en vente",
                            urgence=100 + (ligne["score"] or 0),
                        )
                    )
                elif signal.get("cle") == "detresse":
                    resultats.append(
                        Mouvement(
                            identifiant=ligne["identifiant"],
                            adresse=ligne["adresse"] or "",
                            municipalite=ligne["municipalite"] or "",
                            score=ligne["score"] or 0,
                            genre="detresse",
                            detail=signal.get("libelle") or "Signal de détresse",
                            urgence=90 + (ligne["score"] or 0),
                        )
                    )

        # 3. Relances dues.
        for relance in relances_dues(conn=conn):
            resultats.append(
                Mouvement(
                    identifiant=relance["identifiant"],
                    adresse=relance["adresse"] or "",
                    municipalite=relance["municipalite"] or "",
                    score=relance["score"] or 0,
                    genre="relance_due",
                    detail=relance["motif"]
                    + (" — EN RETARD" if relance["en_retard"] else ""),
                    urgence=(120 if relance["en_retard"] else 80),
                )
            )

        # Déduplication : un dossier peut cumuler plusieurs mouvements, on garde
        # le plus urgent pour ne pas noyer la liste.
        par_dossier: dict[str, Mouvement] = {}
        for mouvement in resultats:
            existant = par_dossier.get(mouvement.identifiant)
            if existant is None or mouvement.urgence > existant.urgence:
                par_dossier[mouvement.identifiant] = mouvement

        tries = sorted(par_dossier.values(), key=lambda m: -m.urgence)
        log.info("foncier.suivi: %d mouvement(s) sur %d jours", len(tries), jours)
        return tries

    finally:
        if fermer:
            conn.close()


def tableau_pipeline(conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    Portrait du pipeline par statut, pour la vue kanban.

    Returns:
        {statut: [dossiers], ...}
    """
    fermer = conn is None
    conn = conn or connexion()
    colonnes = ("nouveau", "surveille", "enrichi", "en_vente", "contacte", "rejete")
    resultat: dict[str, list[dict]] = {statut: [] for statut in colonnes}

    try:
        lignes = conn.execute(
            "SELECT identifiant, adresse, municipalite, score, valeur_totale, "
            "       statut, proprietaire, cap_rate_estime, maj_le "
            "FROM immeubles ORDER BY score DESC LIMIT 500"
        ).fetchall()

        for ligne in lignes:
            statut = ligne["statut"] if ligne["statut"] in resultat else "nouveau"
            resultat[statut].append(
                {
                    "identifiant": ligne["identifiant"],
                    "adresse": ligne["adresse"],
                    "municipalite": ligne["municipalite"],
                    "score": ligne["score"],
                    "valeur_totale": ligne["valeur_totale"],
                    "proprietaire": ligne["proprietaire"],
                    "cap_rate_estime": ligne["cap_rate_estime"],
                    "maj_le": ligne["maj_le"],
                }
            )
        return resultat
    finally:
        if fermer:
            conn.close()
