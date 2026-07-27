#!/usr/bin/env python3
"""
scripts/test_foncier.py — Vérification du moteur de prospection foncière.

Teste le moteur sur des données synthétiques, sans réseau ni clé d'API. À lancer
après toute modification de `criteres.py` pour vérifier que la thèse produit
encore le classement attendu.

    python scripts/test_foncier.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.foncier import finance, scoring, store  # noqa: E402
from core.foncier.criteres import DEFAUT, SCORE_MAX  # noqa: E402
from core.foncier.geo import IndexSpatial, distance_m, point_dans_geometrie  # noqa: E402
from core.foncier.modele import Immeuble, Signal  # noqa: E402
from core.foncier.sources import roles, ventes_taxes  # noqa: E402

echecs: list[str] = []


def verifier(condition: bool, description: str) -> None:
    if condition:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description}")
        echecs.append(description)


# ─────────────────────────────────────────────────────────────────────────────
print("\n1. Modèle et identité")

multiplex = Immeuble(
    lot="5 123 456",
    adresse="1450 rue Ontario Est",
    municipalite="Montréal",
    cubf=1000,
    nombre_logements=12,
    nombre_etages=3,
    annee_construction=1962,
    superficie_terrain=800.0,
    superficie_batiment=960.0,
    valeur_totale=2_400_000.0,
    latitude=45.5320,
    longitude=-73.5510,
    millesime=2026,
)

verifier(len(multiplex.identifiant) == 16, "identifiant stable généré depuis le lot")
verifier(
    multiplex.identifiant == Immeuble(lot="5 123 456").identifiant,
    "même lot → même identifiant (déduplication entre sources)",
)
verifier(multiplex.est_multiresidentiel, "12 logements reconnus comme multirésidentiel")
verifier(multiplex.type_immeuble == "multiresidentiel", "type déduit correctement")
verifier(
    abs((multiplex.ratio_occupation_sol or 0) - 1.2) < 0.01,
    "ratio bâti/terrain calculé (960/800 = 1.2)",
)

sans_identite = Immeuble(cubf=1000)
try:
    sans_identite.identifiant
    verifier(False, "immeuble sans identité doit lever ValueError")
except ValueError:
    verifier(True, "immeuble sans identité rejeté explicitement")

# ─────────────────────────────────────────────────────────────────────────────
print("\n2. Filtres de la thèse")

ok, motif = scoring.eligible(multiplex)
verifier(ok, "multiplex 2,4 M$ à 12 logements retenu")

trop_petit = Immeuble(lot="1", municipalite="Montréal", cubf=1000, nombre_logements=2,
                      valeur_totale=600_000)
ok, motif = scoring.eligible(trop_petit)
verifier(not ok, f"duplex 600 k$ écarté ({motif})")

trop_cher = Immeuble(lot="2", municipalite="Montréal", cubf=1000, nombre_logements=40,
                     valeur_totale=18_000_000)
ok, motif = scoring.eligible(trop_cher)
verifier(not ok, f"immeuble 18 M$ écarté ({motif})")

industriel = Immeuble(lot="3", municipalite="Laval", cubf=2300, valeur_totale=3_000_000)
ok, motif = scoring.eligible(industriel)
verifier(not ok, f"industriel écarté ({motif})")

commercial = Immeuble(lot="4", municipalite="Longueuil", cubf=5300, valeur_totale=3_500_000,
                      superficie_terrain=2000, superficie_batiment=400)
ok, _ = scoring.eligible(commercial)
verifier(ok, "commercial 3,5 M$ retenu")

terrain = Immeuble(lot="9", municipalite="Longueuil", cubf=9100, valeur_totale=450_000,
                   superficie_terrain=3000)
ok, _ = scoring.eligible(terrain)
verifier(ok, "terrain vague retenu en filière développement malgré 450 k$")

# La tolérance rôle/marché doit capturer sous la bande nominale.
sous_bande = Immeuble(lot="10", municipalite="Montréal", cubf=1000, nombre_logements=6,
                      valeur_totale=850_000)
ok, _ = scoring.eligible(sous_bande)
verifier(ok, "immeuble à 850 k$ capturé par la tolérance de 25 %")

# ─────────────────────────────────────────────────────────────────────────────
print("\n3. Sous-utilisation du terrain")

verifier(
    scoring.intensite_sous_utilisation(terrain) == 1.0,
    "terrain vague → sous-utilisation maximale",
)
verifier(
    scoring.intensite_sous_utilisation(commercial) > 0.5,
    "commercial de plain-pied sur grand terrain → forte sous-utilisation",
)
verifier(
    scoring.intensite_sous_utilisation(multiplex) == 0.0,
    "multiplex dense (ratio 1.2) → aucune sous-utilisation",
)

# ─────────────────────────────────────────────────────────────────────────────
print("\n4. Scoring")

multiplex.ajouter_signal(Signal("aire_tod", "Aire TOD Frontenac", "test", intensite=1.0))
multiplex.ajouter_signal(Signal("zone_ppu", "PPU Sainte-Marie", "test", intensite=1.0))
multiplex.date_acquisition = "1994-06-15"

score = scoring.scorer(multiplex)
verifier(score > 0, f"score calculé : {score}/{SCORE_MAX}")
verifier("aire_tod" in multiplex.detail_score, "signal TOD compté dans le détail")
verifier("zone_ppu" in multiplex.detail_score, "signal PPU compté dans le détail")
verifier(
    "detention_longue" in multiplex.detail_score,
    "détention de 30+ ans dérivée automatiquement de la date d'acquisition",
)
verifier(score <= SCORE_MAX, "score plafonné à 100")

recent = Immeuble(lot="11", municipalite="Montréal", cubf=1000, nombre_logements=8,
                  valeur_totale=2_000_000, date_acquisition="2023-01-10")
scoring.scorer(recent)
verifier(
    "detention_longue" not in recent.detail_score,
    "acquisition de 2023 → pas de signal de détention longue",
)

# Un signal inconnu ne doit pas planter le scoring.
bizarre = Immeuble(lot="12", municipalite="Laval", cubf=1000, nombre_logements=6,
                   valeur_totale=1_500_000)
bizarre.ajouter_signal(Signal("signal_inexistant", "test", "test"))
scoring.scorer(bizarre)
verifier(True, "signal sans poids ignoré sans planter (avertissement journalisé)")

explication = scoring.expliquer(multiplex)
verifier("Score" in explication and "aire tod" in explication.lower(),
         "explication du score lisible et détaillée")

# ─────────────────────────────────────────────────────────────────────────────
print("\n5. Classement et rejets")

# Retenus attendus : multiplex, commercial, terrain, recent.
# Rejetés attendus : trop_petit (2 logements), trop_cher (18 M$), industriel (CUBF 2300).
lot_test = [multiplex, trop_petit, trop_cher, industriel, commercial, terrain, recent]
retenus, rejets = scoring.classer(lot_test)

verifier(len(retenus) == 4, f"4 retenus sur 7 (obtenu : {len(retenus)})")
verifier(len(rejets) == 3, f"3 rejets avec motif (obtenu : {len(rejets)})")
verifier(
    retenus[0].score >= retenus[-1].score,
    "tri par score décroissant",
)
resume = scoring.resume_rejets(rejets)
verifier("écarté" in resume, "résumé des rejets agrégé par motif")

# ─────────────────────────────────────────────────────────────────────────────
print("\n6. Estimation financière")

estimation = finance.estimer(multiplex)
verifier(estimation.noi is not None, f"NOI estimé : {estimation.noi:,.0f} $")
verifier(estimation.cap_rate is not None, f"cap rate estimé : {estimation.cap_rate:.2%}")
verifier(estimation.confiance == "moyenne", "confiance « moyenne » sur reconstruction du rôle")
verifier("12 logements" in estimation.methode, "méthode de calcul traçable")

reel = finance.estimer(multiplex, revenu_brut_reel=280_000, depenses_reelles=110_000)
verifier(reel.confiance == "forte", "confiance « forte » avec états financiers réels")
verifier(
    abs(reel.noi - (280_000 * 0.97 - 110_000)) < 1,
    "NOI réel = revenu effectif − dépenses réelles",
)

vide = finance.estimer(Immeuble(lot="13", municipalite="Gaspé", cubf=5300,
                                valeur_totale=1_200_000))
verifier(vide.confiance == "aucune", "aucune confiance quand rien n'est estimable")
verifier(vide.noi is None, "NOI absent plutôt qu'inventé")

prix_max = finance.prix_maximal(150_000, 0.075)
verifier(abs(prix_max - 2_000_000) < 1, "prix maximal pour un cap rate cible de 7,5 %")

# ─────────────────────────────────────────────────────────────────────────────
print("\n7. Géométrie")

carre = {
    "type": "Polygon",
    "coordinates": [[[-73.60, 45.50], [-73.50, 45.50], [-73.50, 45.60], [-73.60, 45.60], [-73.60, 45.50]]],
}
verifier(point_dans_geometrie(45.55, -73.55, carre), "point à l'intérieur du polygone")
verifier(not point_dans_geometrie(45.45, -73.55, carre), "point à l'extérieur du polygone")

troue = {
    "type": "Polygon",
    "coordinates": [
        [[-73.60, 45.50], [-73.50, 45.50], [-73.50, 45.60], [-73.60, 45.60], [-73.60, 45.50]],
        [[-73.57, 45.53], [-73.53, 45.53], [-73.53, 45.57], [-73.57, 45.57], [-73.57, 45.53]],
    ],
}
verifier(not point_dans_geometrie(45.55, -73.55, troue), "point dans un trou exclu")

distance = distance_m(45.5017, -73.5673, 45.5088, -73.5540)
verifier(1000 < distance < 1400, f"distance Montréal centre-ville ≈ {distance:.0f} m")

index = IndexSpatial()
index.ajouter("Aire test", carre, {"rayon": 500})
verifier(len(index.chercher(45.55, -73.55)) == 1, "index spatial retrouve la zone")
verifier(len(index.chercher(40.0, -70.0)) == 0, "index spatial rejette un point lointain")

# ─────────────────────────────────────────────────────────────────────────────
print("\n8. Lecture des rôles — tolérance de schéma")

correspondance = roles.construire_correspondance(
    ["CIVIQUE_DEBUT", "NOM_RUE", "MUNICIPALITE", "CODE_UTILISATION",
     "NOMBRE_LOGEMENT", "SUPERFICIE_TERRAIN", "MATRICULE83"]
)
verifier(correspondance.get("cubf") == "CODE_UTILISATION", "alias CUBF reconnu")
verifier(correspondance.get("nombre_logements") == "NOMBRE_LOGEMENT", "alias logements reconnu")

variante = roles.construire_correspondance(
    ["no_civique_debut", "rue", "nom_municipalite", "cubf", "nb_logements"]
)
verifier(variante.get("cubf") == "cubf", "alias reconnu en minuscules")
verifier(variante.get("municipalite") == "nom_municipalite", "variante de nom de municipalité")

with tempfile.TemporaryDirectory() as tmp:
    csv_test = Path(tmp) / "role.csv"
    csv_test.write_text(
        "CIVIQUE_DEBUT,NOM_RUE,MUNICIPALITE,CODE_UTILISATION,NOMBRE_LOGEMENT,"
        "SUPERFICIE_TERRAIN,SUPERFICIE_BATIMENT,VALEUR_TOTALE,MATRICULE83\n"
        "1450,rue Ontario Est,Montréal,1000,12,800,960,2 400 000,9999-88-7777\n"
        "200,boulevard Taschereau,Longueuil,5300,,2000,400,3 500 000,1111-22-3333\n",
        encoding="utf-8",
    )
    charges = roles.charger_fichier(csv_test, millesime=2026)
    verifier(len(charges) == 2, "CSV lu — 2 immeubles")
    verifier(charges[0].valeur_totale == 2_400_000, "valeur avec espaces insécables parsée")
    verifier(charges[0].adresse == "1450 rue Ontario Est", "adresse composée correctement")
    verifier(charges[1].municipalite == "Longueuil", "municipalité lue")

    mauvais = Path(tmp) / "inconnu.csv"
    mauvais.write_text("colonne_a,colonne_b\n1,2\n", encoding="utf-8")
    try:
        roles.charger_fichier(mauvais)
        verifier(False, "schéma inconnu doit lever SchemaInconnu")
    except roles.SchemaInconnu as e:
        verifier("colonne_a" in str(e), "erreur de schéma affiche les colonnes réelles")

# ─────────────────────────────────────────────────────────────────────────────
print("\n9. Comparaison de millésimes")

ancien = [Immeuble(lot="A", municipalite="Laval", cubf=1000, nombre_logements=6,
                   valeur_totale=1_000_000, millesime=2023)]
nouveau = [
    Immeuble(lot="A", municipalite="Laval", cubf=1000, nombre_logements=12,
             valeur_totale=1_800_000, millesime=2026),
    Immeuble(lot="B", municipalite="Laval", cubf=1000, nombre_logements=8,
             valeur_totale=2_000_000, millesime=2026),
]
delta = roles.comparer_millesimes(ancien, nouveau)
verifier(len(delta["apparus"]) == 1, "immeuble apparu détecté")
verifier(len(delta["revalorises"]) == 1, "revalorisation de 80 % détectée")
verifier(len(delta["restructures"]) == 1, "passage de 6 à 12 logements détecté")

# ─────────────────────────────────────────────────────────────────────────────
print("\n10. Avis de vente pour taxes")

avis = """
AVIS DE VENTE POUR DEFAUT DE PAIEMENT DE TAXES
Lot 5 123 456, 1450 rue Ontario Est, arrieres de 12 450,32 $
Lot 4 987 654, 88 chemin du Lac, arrieres de 3 200,00 $
Cette ligne ne contient aucun numero de lot valide
"""
trouves = ventes_taxes.analyser_texte(avis, "MRC de Test", "https://exemple.ca/avis.pdf")
verifier(len(trouves) == 2, f"2 lots extraits de l'avis (obtenu : {len(trouves)})")
verifier(trouves[0].lot == "5 123 456", "numéro de lot normalisé")
verifier(trouves[0].a_signal("detresse"), "signal de détresse attaché")
verifier(
    trouves[0].signaux[0].donnees.get("arrieres") == 12450.32,
    "montant des arriérés extrait au format québécois",
)
verifier("Ontario" in trouves[0].adresse, "adresse civique extraite")

rapport_vide = ventes_taxes.rapport([
    ventes_taxes.ResultatMRC("MRC A", trouves, "https://x.ca/a.pdf", "ok"),
    ventes_taxes.ResultatMRC("MRC B", [], "https://x.ca/b.pdf", "extraction_impossible", "PDF image"),
])
verifier("À LIRE À LA MAIN" in rapport_vide, "MRC non extraite signalée, jamais silencieuse")
verifier("NON incluse" in rapport_vide or "NON couverte" in rapport_vide.replace("é", "e")
         or "non couverte" in rapport_vide.lower(), "couverture partielle explicitement dite")

# ─────────────────────────────────────────────────────────────────────────────
print("\n11. Persistance")

with tempfile.TemporaryDirectory() as tmp:
    chemin_db = Path(tmp) / "test.db"
    conn = store.connexion(chemin_db)

    resultat = store.enregistrer([multiplex, commercial], conn)
    verifier(resultat["nouveaux"] == 2, "2 immeubles insérés")

    relu = store.charger(multiplex.identifiant, conn)
    verifier(relu is not None, "immeuble relu depuis la base")
    verifier(relu.score == multiplex.score, "score persisté")
    verifier(len(relu.signaux) == len(multiplex.signaux), "signaux persistés")

    # Une passe gratuite ne doit pas écraser un enrichissement payé.
    multiplex.proprietaire = "9123-4567 Québec inc."
    multiplex.cout_enrichissement = 2.50
    store.enregistrer([multiplex], conn)

    appauvri = Immeuble(lot="5 123 456", adresse="1450 rue Ontario Est",
                        municipalite="Montréal", cubf=1000, nombre_logements=12,
                        valeur_totale=2_500_000, millesime=2027)
    store.enregistrer([appauvri], conn)
    apres = store.charger(multiplex.identifiant, conn)
    verifier(
        apres.proprietaire == "9123-4567 Québec inc.",
        "propriétaire payé préservé malgré une passe gratuite",
    )
    verifier(apres.valeur_totale == 2_500_000, "valeur au rôle mise à jour par la passe fraîche")

    # Statut manuel préservé.
    store.marquer(commercial.identifiant, statut="contacte", note="appel du 3 juillet", conn=conn)
    store.enregistrer([Immeuble(lot="4", municipalite="Longueuil", cubf=5300,
                                valeur_totale=3_600_000)], conn)
    suivi = store.charger(commercial.identifiant, conn)
    verifier(suivi.statut == "contacte", "statut manuel préservé")
    verifier(suivi.note == "appel du 3 juillet", "note manuelle préservée")

    meilleurs = store.meilleurs(10, 0, conn=conn)
    verifier(len(meilleurs) == 2, "lecture des meilleurs dossiers")
    verifier(
        meilleurs[0].score >= meilleurs[1].score,
        "meilleurs triés par score décroissant",
    )

    filtres = store.meilleurs(10, 0, None, "Longueuil", conn=conn)
    verifier(len(filtres) == 1, "filtre par municipalité")

    stats = store.statistiques(conn)
    verifier(stats["total"] == 2, "statistiques cohérentes")

    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
print("\n12. Garde-fou budgétaire")

from core.foncier.sources.registre import Budget  # noqa: E402

budget = Budget(plafond=10.0)
verifier(budget.autoriser(2.5), "appel autorisé sous le plafond")
for _ in range(4):
    budget.enregistrer(2.5)
verifier(not budget.autoriser(2.5), "appel refusé une fois le plafond atteint")
verifier("NON EFFECTUÉ" in budget.resume(), "refus budgétaire annoncé, jamais silencieux")
verifier(budget.depense == 10.0, "dépense totalisée exactement")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "═" * 60)
if echecs:
    print(f"❌ {len(echecs)} vérification(s) en échec :")
    for echec in echecs:
        print(f"   · {echec}")
    sys.exit(1)

print("✅ Toutes les vérifications passent.")
print(f"   Thèse : {DEFAUT.prix_min/1e6:.0f}-{DEFAUT.prix_max/1e6:.0f} M$ · "
      f"NOI {DEFAUT.noi_cible_min:.0%}-{DEFAUT.noi_cible_max:.0%} · "
      f"détention {DEFAUT.detention_seuil} ans+ · score sur {SCORE_MAX}")
sys.exit(0)
