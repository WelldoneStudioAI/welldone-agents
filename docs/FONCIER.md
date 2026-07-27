# Agent Foncier — prospection immobilière Québec

Trouver des immeubles commerciaux et multirésidentiels de 1 à 5 M$ avec potentiel
de développement, **avant** qu'ils arrivent sur Centris.

---

## 1. Le malentendu à lever en premier

Le Registre foncier du Québec **ne contient aucun immeuble à vendre**. Il publie
des *droits* : ventes conclues, hypothèques, servitudes, préavis. Zéro annonce.
Un agent qui « fouille le registre à la recherche d'immeubles à vendre » ne
trouve rien.

Le système inverse donc la logique. Le registre ne dit pas ce qui est à vendre,
il dit **ce qui va se vendre**.

Deux contraintes structurent toute l'architecture :

| Contrainte | Conséquence dans le code |
|---|---|
| Le registre s'interroge **par immeuble**, pas en lot. Aucun flux « tous les préavis de la semaine ». | On construit une *watchlist* qu'on surveille, pas un aspirateur. La recherche inversée passe par JLR. |
| Les conditions d'utilisation **interdisent l'extraction automatisée massive**. | `sources/registre.py` n'appelle jamais `registrefoncier.gouv.qc.ca`. Il passe par des fournisseurs sous entente (fonciq, JLR), ou il ne fait rien. |

---

## 2. La thèse encodée

Source : conversation Cedrik, juillet 2026. Encodée dans
[`core/foncier/criteres.py`](../core/foncier/criteres.py) — **seul fichier à
modifier** pour changer la stratégie.

| Critère | Valeur | Où |
|---|---|---|
| Types d'immeubles | Commercial + multirésidentiel (5 logements et plus) | `GROUPES_CIBLES` |
| Bande de prix | 1 M$ à 5 M$ | `PRIX_MIN` / `PRIX_MAX` |
| Tolérance de capture | ±25 % (écart rôle/marché) → 0,75 à 6,25 M$ | `PRIX_TOLERANCE` |
| NOI visé | 7 % à 8 % | `NOI_CIBLE_MIN` / `MAX` |
| Détention | 25 ans et plus | `DETENTION_SEUIL_ANNEES` |
| Secteurs | Aires TOD, zones PPU | `TOD_RAYON_*` |
| Signaux amont | SEAO, préavis d'exercice | `SEAO_MOTS_CLES` |

### Répartition du score (100 points)

| Points | Signal | Source | Coût |
|---:|---|---|---|
| 25 | Détention de 25 ans et plus | registre foncier | **payant** |
| 20 | Situé dans une aire TOD | CMM / points de transport | gratuit |
| 15 | Zone PPU active | municipal | gratuit |
| 15 | Terrain sous-utilisé | rôle d'évaluation | gratuit |
| 15 | Détresse financière | ventes pour taxes, préavis | gratuit / payant |
| 10 | Investissement public à proximité | SEAO | gratuit |

**Sans configuration payante, le plafond réel est de 60 points sur 100.** La
commande `foncier sources` le dit explicitement plutôt que de laisser croire à
un marché pauvre.

---

## 3. Architecture

```
Sources gratuites                     Scoring                 Dépense
─────────────────                     ───────                 ───────
rôles d'évaluation ──┐
ventes pour taxes ───┼──► normalisation ──► filtres de ──► score 0-100
SEAO ────────────────┤    (Immeuble)        la thèse          │
aires TOD / PPU ─────┘                                        │
                                                    ┌─────────┴─────────┐
                                              score < 45          score ≥ 45
                                                    │                   │
                                              reste en base    enrichissement
                                              (gratuit)        registre foncier
                                                                        │
                                                                  score ≥ 60
                                                                        │
                                                              brief Claude
                                                              → Notion + Telegram
```

**La règle économique du système** : on ne paie jamais pour *découvrir*,
seulement pour *confirmer*. Le scoring tourne gratuitement sur des centaines de
milliers d'unités d'évaluation ; l'enrichissement ne touche que la dizaine de
dossiers qui ont passé le seuil. C'est la différence entre 40 $ et 4 000 $ par
mois.

### Pourquoi le scoring n'est pas fait par un LLM

Il est en Python pur, délibérément :

1. **Reproductible** — même immeuble, même score. Le classement est défendable
   et les poids sont *backtestables* sur les millésimes 2008-2025 du rôle.
2. **Gratuit** — on score tout le parc à chaque passe.
3. **Auditable** — `detail_score` explique chaque point. `foncier top --detail`
   l'affiche.

Claude n'intervient qu'à la toute fin, sur 8 dossiers déjà classés, pour
rédiger. C'est là qu'il est bon.

---

## 4. Commandes

```bash
python dispatch.py foncier sources        # état de configuration — à lancer en premier
python dispatch.py foncier scan           # ventes pour taxes des MRC (gratuit)
python dispatch.py foncier seao           # avis d'appel d'offres → signaux de secteur
python dispatch.py foncier roles --fichier data/role.geojson --millesime 2026
python dispatch.py foncier top --limite 15 --detail
python dispatch.py foncier top --municipalite Longueuil --score 50
python dispatch.py foncier enrichir --limite 10 --budget 25    # PAYANT
python dispatch.py foncier brief --limite 8                    # → Notion + Telegram
python dispatch.py foncier cubf --fichier data/role.csv        # valider les codes
python dispatch.py foncier marquer --id a1b2c3 --statut contacte --note "appel du 3 juillet"
```

Toutes acceptent `--telegram` et `--json`.

### Automatisations

| Cron (UTC) | Heure Montréal | Commande |
|---|---|---|
| `0 10 * * 2` | mardi 6 h | `scan` — ventes pour taxes |
| `0 10 * * 4` | jeudi 6 h | `seao` — nouveaux avis |
| `0 11 * * 1` | lundi 7 h | `brief` — dossiers du jour |

L'enrichissement payant n'est **jamais** automatique. Il se déclenche à la main.

---

## 5. Mise en route

### Sprint 1 — gratuit, une soirée

```bash
python dispatch.py foncier sources
python dispatch.py foncier scan --telegram
```

Dix MRC sont déjà au registre. Le fichier
[`sources/ventes_taxes.py`](../core/foncier/sources/ventes_taxes.py) en attend
75 autres — c'est de la saisie, pas du développement, et c'est le meilleur
retour sur temps investi du projet.

### Sprint 2 — le socle

Télécharger un rôle sur [Données Québec][roles] (1 140 fichiers XML, millésimes
2008 à 2026), puis :

```bash
python dispatch.py foncier cubf --fichier data/role.csv --limite 50000
python dispatch.py foncier roles --fichier data/role.csv --millesime 2026
```

`cubf` d'abord : il affiche la distribution réelle des codes d'utilisation de la
municipalité et permet de valider les hypothèses de `criteres.py` avant de
lancer une campagne dessus.

### Sprint 3 — le zonage

Récupérer les aires TOD sur l'[Observatoire du Grand Montréal][cmm] et
renseigner `FONCIER_TOD_POLYGONES_URL`. À défaut de polygones, une simple liste
de points d'accès au transport suffit : le module dérive les aires par rayon
500 m / 1 km, exactement comme la CMM les construit.

Gain : 35 points de scoring débloqués.

### Sprint 4 — le payant

Ouvrir un compte [fonciq][fonciq] (inscription gratuite, 5 crédits offerts) et
valider la qualité de l'enrichissement sur les 5 meilleurs dossiers **avant** de
s'engager sur du volume. Leur API v1 était annoncée en préparation pour
partenaires pilotes — ça vaut un courriel.

Pour la recherche inversée de préavis d'exercice par territoire — celle qu'on ne
peut pas faire soi-même sur le registre public — c'est JLR qu'il faut.

---

## 6. Ce que le système ne fait pas, et pourquoi

| Non fait | Raison |
|---|---|
| Scraper `registrefoncier.gouv.qc.ca` | Les conditions d'utilisation interdisent l'extraction automatisée massive. |
| Scraper Centris / DuProprio | Mêmes interdictions contractuelles. Centris reste une consultation manuelle, ou via un courtier. |
| Inventer une valeur manquante | Un champ absent reste `None` et le brief le dit. Un chiffre inventé dans un dossier d'acquisition est pire que pas de chiffre. |
| Contacter les propriétaires | Voir la section suivante. |

### Les estimations financières ne sont pas des chiffres d'offre

Le rôle d'évaluation ne contient **aucun revenu**. `finance.py` reconstruit un
NOI à partir du nombre de logements ou de la superficie, avec des hypothèses de
loyer documentées dans le module. Chaque estimation porte son niveau de
confiance :

- `forte` — états financiers réels fournis par le vendeur
- `moyenne` — reconstruit du nombre de logements réel
- `faible` — reconstruit de la superficie seule (optimiste : la superficie
  locative est toujours inférieure à la superficie bâtie)
- `aucune` — non estimable

Ces chiffres servent à **classer** les dossiers, jamais à faire une offre. Les
hypothèses de loyer sont à réviser annuellement contre l'Enquête sur les
logements locatifs de la SCHL.

---

## 7. Les trois murs légaux

**Conditions du Registre foncier** — pas d'extraction automatisée massive. Le
code passe par des fournisseurs sous entente. Ne pas contourner : c'est le seul
point où le projet peut mal tourner.

**Loi 25** — le nom d'un propriétaire physique est un renseignement personnel.
La [Commission d'accès à l'information][cai] est explicite : la prospection
commerciale **n'est pas une « fin compatible »** et exige un consentement
préalable. Acheter pour son propre compte et écrire à un propriétaire est très
différent de constituer puis revendre une base de leads — le second expose
sérieusement. Pour les courriels, la LCAP s'ajoute.

**OACIQ** — acheter pour soi ne demande aucun permis. Mettre en relation
acheteur et vendeur contre rémunération, c'est du courtage : permis obligatoire.

---

## 8. Vérification

```bash
python scripts/test_foncier.py
```

60+ vérifications sur données synthétiques, sans réseau ni clé d'API : filtres
de la thèse, scoring, estimations financières, géométrie, tolérance de schéma
des rôles, extraction des avis de vente pour taxes, persistance, garde-fous
budgétaires.

À relancer après **toute** modification de `criteres.py`.

---

## 9. Points de vigilance connus

**Le disque Railway est éphémère.** Sans volume monté, `foncier.db` est perdue à
chaque redéploiement — donc l'historique des scores et la mémoire des dépenses
avec. Monter un volume et pointer `FONCIER_DB_PATH` dessus.

**Les noms de propriétaires sont caviardés dans l'open data.** Le rôle donne le
quoi, jamais le qui. C'est structurel, pas un bogue.

**Le champ « superficie bâtiment » n'a pas le même sens partout.** Emprise au
sol dans certaines municipalités, superficie totale des étages dans d'autres. Le
ratio de sous-utilisation n'est donc comparable qu'à l'intérieur d'une même
source. Vérifier sur quelques immeubles connus avant de se fier au signal sur un
nouveau territoire.

**Une source muette n'est pas une source vide.** Les rapports distinguent
toujours « aucun résultat » de « extraction impossible » et de « injoignable ».
Une MRC dont le PDF n'a pas pu être lu apparaît en `À LIRE À LA MAIN` avec son
lien — jamais silencieusement absente du décompte.

---

## 10. Sources

- [Rôles d'évaluation foncière du Québec][roles] — Données Québec
- [Unités d'évaluation foncière (Montréal)](https://www.donneesquebec.ca/recherche/dataset/vmtl-unites-evaluation-fonciere)
- [Système électronique d'appel d'offres (SEAO)](https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao)
- [Données géoréférencées de la CMM][cmm] — aires TOD
- [fonciq][fonciq] — registre foncier consolidé
- [Loi 25 — Commission d'accès à l'information][cai]

[roles]: https://www.donneesquebec.ca/recherche/dataset/roles-d-evaluation-fonciere-du-quebec
[cmm]: https://observatoire.cmm.qc.ca/produits/donnees-georeferencees/
[fonciq]: https://fonciq.ca/
[cai]: https://www.cai.gouv.qc.ca/protection-renseignements-personnels/sujets-et-domaines-dinteret/principaux-changements-loi-25
