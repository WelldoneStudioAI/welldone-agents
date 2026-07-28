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
from datetime import date, timedelta  # noqa: E402
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
print("\n13. Normalisation d'adresses")

from core.foncier import adresse as adr  # noqa: E402

verifier(
    adr.normaliser_municipalite("Montréal (Rosemont—La Petite-Patrie)") == "montreal",
    "arrondissement entre parenthèses retiré de la municipalité",
)
verifier(
    adr.normaliser_municipalite("Ville de Longueuil") == "longueuil",
    "préfixe « Ville de » retiré",
)
verifier(
    adr.normaliser_municipalite("St-Jean-sur-Richelieu")
    == adr.normaliser_municipalite("Saint-Jean-sur-Richelieu"),
    "Saint et St donnent la même municipalité",
)

# Le cœur du croisement : les mêmes immeubles écrits différemment.
paires_identiques = [
    ("1450 rue Ontario Est", "Montréal", "1450, Ontario E.", "Montreal"),
    ("1450 ONTARIO E", "MONTRÉAL", "1450, rue Ontario Est", "Montréal"),
    ("200 boul. Taschereau", "Longueuil", "200, boulevard Taschereau", "Longueuil"),
    ("55 av. du Parc", "Montréal", "55 avenue Parc", "Montréal"),
    ("12 ch. Saint-Denis", "Laval", "12 chemin St-Denis", "Laval"),
    ("1450-1454 rue Ontario Est", "Montréal", "1450 rue Ontario Est", "Montréal"),
    ("1450 rue Ontario Est, app. 3", "Montréal", "1450 rue Ontario Est", "Montréal"),
]
for adresse_a, mun_a, adresse_b, mun_b in paires_identiques:
    verifier(
        adr.correspond(adresse_a, mun_a, adresse_b, mun_b),
        f"« {adresse_a} » ≡ « {adresse_b} »",
    )

# Ce qui ne doit PAS se rapprocher.
paires_differentes = [
    ("1450 rue Ontario Est", "Montréal", "1452 rue Ontario Est", "Montréal"),
    ("1450 rue Ontario Est", "Montréal", "1450 rue Ontario Est", "Laval"),
    ("1450 rue Ontario Est", "Montréal", "1450 rue Sherbrooke Est", "Montréal"),
    ("100 rue Principale", "Granby", "100 rue Principale", "Sherbrooke"),
]
for adresse_a, mun_a, adresse_b, mun_b in paires_differentes:
    verifier(
        not adr.correspond(adresse_a, mun_a, adresse_b, mun_b),
        f"« {adresse_a} / {mun_a} » ≠ « {adresse_b} / {mun_b} »",
    )

# Le type de voie seul ne doit jamais suffire à rapprocher.
verifier(
    not adr.correspond("100 rue Alpha", "Laval", "100 rue Bravo", "Laval"),
    "deux rues différentes au même numéro ne se rapprochent pas",
)

# L'identifiant de l'immeuble doit hériter de la normalisation.
verifier(
    Immeuble(adresse="1450 rue Ontario Est", municipalite="Montréal").identifiant
    == Immeuble(adresse="1450, Ontario E.", municipalite="Montreal").identifiant,
    "même immeuble, deux graphies → même identifiant",
)

# ─────────────────────────────────────────────────────────────────────────────
print("\n14. Parseur d'alertes Centris")

from core.foncier.sources import centris  # noqa: E402

courriel = """<html><body>
<table><tr><td>
  <a href="https://www.centris.ca/fr/triplex~a-vendre~montreal-rosemont/27384512">
    Triplex &agrave; vendre
  </a><br>
  1450, rue Ontario Est, Montr&eacute;al (Rosemont)<br>
  <strong>2&nbsp;450&nbsp;000&nbsp;$</strong><br>
  3 logements
</td></tr>
<tr><td>
  <a href="https://www.centris.ca/fr/immeuble-a-revenus~a-vendre~longueuil/88112233">
    Immeuble &agrave; revenus
  </a><br>
  200, boulevard Taschereau, Longueuil<br>
  <strong>3&nbsp;500&nbsp;000&nbsp;$</strong><br>
  8 logements
</td></tr></table>
</body></html>"""

diag = centris.Diagnostic()
extraites = centris.extraire(courriel, "Nouvelles inscriptions", diag)

verifier(len(extraites) == 2, f"2 fiches extraites (obtenu : {len(extraites)})")
verifier(
    {f.no_centris for f in extraites} == {"27384512", "88112233"},
    "numéros Centris extraits depuis les URL",
)

premiere = next(f for f in extraites if f.no_centris == "27384512")
verifier("1450" in premiere.adresse and "Ontario" in premiere.adresse,
         f"adresse extraite : « {premiere.adresse} »")
verifier(premiere.prix_demande == 2_450_000, f"prix extrait : {premiere.prix_demande}")
verifier(premiere.nombre_logements == 3, "nombre de logements extrait")
verifier(premiere.type_declare == "triplex", "type d'immeuble reconnu")
verifier(premiere.url.startswith("https://www.centris.ca/"), "URL conservée intacte")

seconde = next(f for f in extraites if f.no_centris == "88112233")
verifier(seconde.prix_demande == 3_500_000, "prix de la seconde fiche")
verifier(seconde.nombre_logements == 8, "8 logements extraits")

# Lien encapsulé dans un traqueur : l'URL réelle est encodée.
courriel_traque = (
    '<a href="https://clic.infolettre.example/r?u='
    "https%3A%2F%2Fwww.centris.ca%2Ffr%2Fduplex~a-vendre~laval%2F55667788"
    '">Voir la fiche</a> 1 800 000 $ 2 logements'
)
diag2 = centris.Diagnostic()
traquees = centris.extraire(courriel_traque, "", diag2)
verifier(
    any(f.no_centris == "55667788" for f in traquees),
    "lien Centris récupéré à travers un redirecteur de suivi",
)

# Un courriel sans fiche ne doit rien inventer.
diag3 = centris.Diagnostic()
vides = centris.extraire("<html><body>Bonjour, rien ici.</body></html>", "", diag3)
verifier(len(vides) == 0, "aucune fiche inventée depuis un courriel vide")
verifier(diag3.fiches_uniques == 0, "diagnostic cohérent sur courriel vide")
verifier("Bonjour" in diag3.echantillon_texte, "échantillon de texte conservé pour calibration")

rapport_diag = diag.rapport()
verifier("Fiches uniques" in rapport_diag, "rapport de calibration lisible")
verifier("2/2" in rapport_diag, "compteurs du diagnostic exacts")

# ─────────────────────────────────────────────────────────────────────────────
print("\n15. Croisement marché × base")

suivi = Immeuble(
    lot="5 123 456",
    adresse="1450 rue Ontario Est",
    municipalite="Montréal",
    cubf=1000,
    nombre_logements=3,
    valeur_totale=2_000_000,
    date_acquisition="1993-04-01",
)
suivi.ajouter_signal(Signal("aire_tod", "Aire TOD Frontenac", "test", intensite=1.0))
scoring.scorer(suivi)

rapprochements, orphelines = centris.rapprocher(extraites, [suivi])

verifier(len(rapprochements) == 1, "la fiche Centris retrouve le dossier suivi")
verifier(len(orphelines) == 1, "la fiche sans correspondance reste orpheline")

rapprochement = rapprochements[0]
verifier(
    rapprochement.immeuble.lot == "5 123 456",
    "le rapprochement pointe le bon immeuble, avec son lot du rôle",
)
verifier(
    rapprochement.ecart_prix is not None
    and abs(rapprochement.ecart_prix - 0.225) < 0.001,
    f"écart prix demandé / valeur au rôle calculé ({rapprochement.ecart_prix:+.1%})",
)
verifier(
    rapprochement.immeuble.a_signal("mise_en_marche"),
    "signal de mise en marché attaché au dossier suivi",
)
verifier(
    "27384512" in rapprochement.resume(),
    "le résumé porte le numéro Centris",
)

# La mise en marché est informative : elle ne doit pas gonfler le score.
score_avant = suivi.score
scoring.scorer(suivi)
verifier(
    suivi.score == score_avant,
    "le signal de mise en marché ne change pas le score (signal informatif)",
)
verifier(
    "mise_en_marche" not in suivi.detail_score,
    "mise_en_marche absent du détail du score",
)

# Une fiche orpheline avec adresse doit pouvoir devenir un dossier.
orpheline = orphelines[0]
nouvel_immeuble = centris.vers_immeuble(orpheline)
verifier(nouvel_immeuble is not None, "fiche orpheline convertie en immeuble")
verifier(nouvel_immeuble.statut == "surveille", "nouveau dossier marqué « surveille »")
verifier(
    nouvel_immeuble.valeur_totale == 3_500_000,
    "prix demandé repris comme valeur du dossier",
)

sans_adresse = centris.FicheCentris(no_centris="1", url="https://www.centris.ca/1")
verifier(
    centris.vers_immeuble(sans_adresse) is None,
    "fiche sans adresse refusée plutôt que créée avec une identité bancale",
)

comparaison = centris.comparables(extraites)
verifier("médiane" in comparaison, "portrait des prix demandés produit")
verifier("logement" in comparaison, "prix par logement calculé")

# ─────────────────────────────────────────────────────────────────────────────
print("\n16. Analyse de transaction")

from core.foncier import montage  # noqa: E402

# Versement hypothécaire : 1 M$ à 5,75 % sur 25 ans ≈ 6 270 $/mois.
versement = montage.paiement_mensuel(1_000_000, 0.0575, 25)
verifier(6_100 < versement < 6_450, f"versement mensuel calculé : {versement:,.0f} $")
verifier(
    montage.paiement_mensuel(0, 0.06, 25) == 0.0,
    "aucun versement sur un emprunt nul",
)

# Droits de mutation, barème de base : 61 500 × 0,5 % + 246 300 × 1 % + reste × 1,5 %.
mutation = montage.droits_mutation(1_000_000)
attendu = 61_500 * 0.005 + (307_800 - 61_500) * 0.01 + (1_000_000 - 307_800) * 0.015
verifier(abs(mutation - attendu) < 1, f"droits de mutation : {mutation:,.0f} $")
verifier(montage.droits_mutation(0) == 0, "aucun droit sur un prix nul")

# Prix pour un cap rate cible — le calcul qui transforme la thèse en offre.
verifier(
    abs(montage.prix_pour_cap_rate(140_000, 0.07) - 2_000_000) < 1,
    "140 k$ de NOI à 7 % → 2 M$",
)
verifier(
    montage.prix_pour_cap_rate(140_000, 0.08) < montage.prix_pour_cap_rate(140_000, 0.07),
    "un cap rate plus élevé exige un prix plus bas",
)

# Plafond du prêteur.
plafond = montage.prix_maximal_finançable(140_000)
verifier(plafond > 0, f"plafond du prêteur calculé : {plafond:,.0f} $")
plafond_verif = montage.Montage("v", plafond, 140_000)
verifier(
    abs(plafond_verif.dscr - montage.DSCR_MINIMUM) < 0.02,
    f"au prix plafond, le DSCR touche exactement le seuil ({plafond_verif.dscr:.2f})",
)
verifier(
    montage.prix_maximal_finançable(0) == 0,
    "aucun plafond calculable sans NOI",
)

# Un montage complet.
m = montage.Montage("Test", 2_000_000, 140_000)
verifier(m.mise_de_fonds == 500_000, "mise de fonds 25 % de 2 M$")
verifier(m.emprunt == 1_500_000, "emprunt = prix − mise de fonds")
verifier(abs(m.cap_rate - 0.07) < 0.001, "cap rate 7 %")
verifier(m.liquidites_requises > m.mise_de_fonds, "liquidités incluent frais et mutation")
verifier(
    abs(m.cashflow_annuel - (m.noi - m.service_dette_annuel)) < 1,
    "cashflow = NOI − service de dette",
)
verifier(m.dscr > 1.0, f"DSCR calculé : {m.dscr:.2f}")

# Un immeuble trop cher pour son NOI doit être déclaré non finançable.
trop_cher = montage.Montage("Trop cher", 4_000_000, 140_000)
verifier(not trop_cher.finançable, "4 M$ pour 140 k$ de NOI → refus probable")
verifier("REFUS" in trop_cher.verdict, "verdict de refus explicite")
verifier(trop_cher.cashflow_annuel < 0, "cashflow négatif détecté")

# Scénarios et sensibilité.
tous = montage.scenarios(2_000_000, 140_000)
verifier(len(tous) == 4, "quatre scénarios de financement")
verifier(
    max(s.dscr for s in tous) > min(s.dscr for s in tous),
    "les scénarios se distinguent par leur DSCR",
)
prudent = next(s for s in tous if "Prudent" in s.nom)
levier = next(s for s in tous if "levier" in s.nom)
verifier(
    prudent.dscr > levier.dscr,
    "plus de mise de fonds → meilleur DSCR",
)
verifier(
    prudent.liquidites_requises > levier.liquidites_requises,
    "plus de mise de fonds → plus de liquidités requises",
)

sens = montage.sensibilite(2_000_000, 140_000)
verifier(len(sens) == 4, "quatre scénarios de sensibilité du NOI")
verifier(
    sens[0]["dscr"] < sens[-1]["dscr"],
    "un NOI plus faible dégrade le DSCR",
)

# Analyse complète sur un immeuble réel du jeu de test.
analyse = montage.analyser(multiplex, prix=2_600_000)
verifier(analyse.offre.noi > 0, "analyse complète produit un NOI")
verifier(len(analyse.montages) == 4, "analyse inclut les scénarios")
verifier(
    any("reconstruit du rôle" in a for a in analyse.avertissements),
    "avertissement sur la provenance du NOI",
)
verifier(
    any("Montréal" in a for a in analyse.avertissements),
    "avertissement sur les droits de mutation montréalais",
)

texte_fiche = montage.fiche(analyse)
verifier("FOURCHETTE D'OFFRE" in texte_fiche, "fiche imprimable produite")
verifier("SCÉNARIOS DE FINANCEMENT" in texte_fiche, "fiche inclut les scénarios")
verifier("À valider" in texte_fiche, "fiche rappelle qu'il faut valider")

# Un immeuble sans revenus estimables ne doit produire aucune offre.
opaque = Immeuble(lot="99", municipalite="Gaspé", cubf=5300, valeur_totale=1_500_000)
analyse_vide = montage.analyser(opaque)
verifier(analyse_vide.offre.noi == 0, "aucune offre calculée sans NOI")
verifier(
    any("NOI non estimable" in a for a in analyse_vide.avertissements),
    "l'impossibilité est dite explicitement",
)
verifier(len(analyse_vide.montages) == 0, "aucun scénario inventé sans NOI")

# ─────────────────────────────────────────────────────────────────────────────
print("\n17. CRM — contacts, interactions, relances")

from core.foncier import suivi  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    chemin_crm = Path(tmp) / "crm.db"
    conn = suivi.connexion(chemin_crm)

    cible = Immeuble(
        lot="7 111 222", adresse="500 rue Sainte-Catherine Est",
        municipalite="Montréal", cubf=1000, nombre_logements=10,
        valeur_totale=3_000_000,
    )
    scoring.scorer(cible)
    store.enregistrer([cible], conn)
    ident = cible.identifiant

    contact_id = suivi.ajouter_contact(
        suivi.Contact(identifiant=ident, nom="Gestion Larose inc.",
                      role="proprietaire", telephone="514-555-0100"),
        conn=conn,
    )
    verifier(contact_id > 0, "contact rattaché au dossier")
    verifier(len(suivi.contacts_du_dossier(ident, conn)) == 1, "contact relu")

    resultat = suivi.journaliser(
        suivi.Interaction(identifiant=ident, canal="telephone",
                          resume="Premier appel, ouvert à discuter",
                          resultat="interesse"),
        conn=conn,
    )
    verifier(resultat["interaction_id"] > 0, "interaction consignée")
    verifier(resultat["statut"] == "contacte", "statut du dossier passé à « contacté »")
    verifier(resultat["relance"] is not None, "relance programmée automatiquement")

    attendue = (date.today() + timedelta(days=suivi.DELAI_RELANCE["interesse"])).isoformat()
    verifier(resultat["relance"] == attendue, "délai de relance conforme au résultat")

    verifier(len(suivi.historique(ident, conn)) == 1, "historique lisible")

    # Canal ou résultat inconnu doit lever plutôt que d'écrire n'importe quoi.
    for mauvais in (
        suivi.Interaction(identifiant=ident, canal="pigeon", resume="x"),
        suivi.Interaction(identifiant=ident, canal="courriel", resume="x", resultat="peut-etre"),
    ):
        try:
            suivi.journaliser(mauvais, conn=conn)
            verifier(False, "valeur invalide doit lever ValueError")
        except ValueError:
            verifier(True, f"valeur invalide rejetée ({mauvais.canal}/{mauvais.resultat})")

    # Un refus ne ferme pas le dossier — il le remet en surveillance.
    suivi.journaliser(
        suivi.Interaction(identifiant=ident, canal="telephone",
                          resume="Pas intéressé cette année", resultat="refus"),
        conn=conn,
    )
    verifier(
        store.charger(ident, conn).statut == "surveille",
        "un refus renvoie le dossier en surveillance, pas à la poubelle",
    )

    # Relance manuelle et clôture.
    date_relance = suivi.programmer_relance(ident, 0, "Rappeler ce matin", conn=conn)
    verifier(date_relance == date.today().isoformat(), "relance manuelle datée du jour")

    dues = suivi.relances_dues(conn=conn)
    verifier(len(dues) >= 1, f"{len(dues)} relance(s) due(s) détectée(s)")
    verifier(dues[0]["adresse"] == cible.adresse, "la relance porte l'adresse du dossier")

    verifier(suivi.marquer_relance_faite(dues[0]["relance_id"], conn), "relance clôturée")
    verifier(
        not suivi.marquer_relance_faite(dues[0]["relance_id"], conn),
        "une relance déjà faite ne se clôture pas deux fois",
    )

    # Mouvements — ce qui mérite une action.
    en_vente = Immeuble(
        lot="8 333 444", adresse="900 avenue du Mont-Royal Est",
        municipalite="Montréal", cubf=1000, nombre_logements=8,
        valeur_totale=2_800_000,
    )
    en_vente.ajouter_signal(
        Signal("mise_en_marche", "Sur Centris — fiche #99887766", "centris_alerte",
               date_signal=date.today().isoformat())
    )
    scoring.scorer(en_vente)
    store.enregistrer([en_vente], conn)

    bouge = suivi.mouvements(21, conn)
    verifier(len(bouge) >= 1, f"{len(bouge)} mouvement(s) détecté(s)")
    genres = {m.genre for m in bouge}
    verifier("mise_en_marche" in genres, "mise en marché remontée dans les mouvements")
    verifier(
        bouge[0].urgence >= bouge[-1].urgence,
        "mouvements triés par urgence décroissante",
    )
    verifier(
        len({m.identifiant for m in bouge}) == len(bouge),
        "un dossier n'apparaît qu'une fois, avec son mouvement le plus urgent",
    )

    tableau = suivi.tableau_pipeline(conn)
    verifier(set(tableau) >= {"nouveau", "surveille", "contacte", "rejete"},
             "pipeline structuré par statut")
    total_pipeline = sum(len(v) for v in tableau.values())
    verifier(total_pipeline == 2, f"2 dossiers répartis dans le pipeline (obtenu : {total_pipeline})")

    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
print("\n18. Registre des MRC extensible")

with tempfile.TemporaryDirectory() as tmp:
    fichier_mrc = Path(tmp) / "mrc.json"
    original = ventes_taxes.FICHIER_MRC
    ventes_taxes.FICHIER_MRC = fichier_mrc
    try:
        base = ventes_taxes.charger_registre()
        verifier(len(base) == len(ventes_taxes.REGISTRE_MRC),
                 f"{len(base)} MRC intégrées au code")

        total = ventes_taxes.ajouter_mrc(
            "MRC de Rouville", "https://exemple.qc.ca/vente-pour-taxes/", "Montérégie"
        )
        verifier(total == len(base) + 1, "MRC ajoutée au registre local")
        verifier(fichier_mrc.exists(), "registre local écrit sur disque")

        etendu = ventes_taxes.charger_registre()
        verifier(any(s.nom == "MRC de Rouville" for s in etendu), "MRC locale relue")

        # Réajouter la même ne doit pas créer de doublon.
        ventes_taxes.ajouter_mrc("MRC de Rouville", "https://autre.qc.ca/avis/")
        final = ventes_taxes.charger_registre()
        verifier(len(final) == len(base) + 1, "pas de doublon sur réajout")
        rouville = next(s for s in final if s.nom == "MRC de Rouville")
        verifier(rouville.url_page == "https://autre.qc.ca/avis/", "URL mise à jour")

        for mauvais in (("", "https://x.ca"), ("MRC X", ""), ("MRC X", "pas-une-url")):
            try:
                ventes_taxes.ajouter_mrc(*mauvais)
                verifier(False, f"entrée invalide doit lever : {mauvais}")
            except ValueError:
                verifier(True, f"entrée invalide rejetée : {mauvais}")
    finally:
        ventes_taxes.FICHIER_MRC = original

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
