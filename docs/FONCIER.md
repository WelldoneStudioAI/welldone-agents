# Agent Foncier — prospection immobilière Québec

Trouver des immeubles commerciaux et multirésidentiels de 1 à 5 M$ avec potentiel
de développement, **avant** qu'ils arrivent sur Centris.

---

## 0. Démarrer en trois commandes

```bash
python dispatch.py foncier sources     # ce qui est branché, ce qui manque
python dispatch.py foncier ingerer     # remplit la base : rôles, MRC, SEAO
python dispatch.py foncier aujourdhui  # ce qui mérite une action
```

Puis l'interface : **`https://<ton-domaine>/foncier/`**

| Onglet | Ce qu'on y fait |
|---|---|
| Aujourd'hui | mises en marché, détresse, scores en hausse, relances dues |
| Dossiers | liste filtrable par score, municipalité, statut |
| Carte | candidats géolocalisés, taille et couleur selon le score |
| Pipeline | kanban nouveau → surveillé → en vente → contacté |
| Thèse | critères actifs, répartition du score, état de configuration |

Le panneau de détail d'un dossier porte le score expliqué signal par signal, le
**calculateur d'offre**, le journal des contacts et la fiche imprimable.

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

Le plafond réel dépend de ce qui est configuré :

| Configuration | Points atteignables |
|---|---|
| Sources gratuites seules | **40 / 100** |
| + zonage (TOD, PPU) | **65 / 100** |
| + registre foncier | **100 / 100** |

`foncier sources` affiche l'état réel plutôt que de laisser croire à un marché
pauvre quand c'est la configuration qui manque.

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
python dispatch.py foncier ingerer        # tout charger d'un coup
python dispatch.py foncier aujourdhui     # ce qui bouge et mérite une action
python dispatch.py foncier mrc            # registre des MRC
python dispatch.py foncier mrc --ajouter "MRC de Rouville" --url https://…
python dispatch.py foncier analyse --id a1b2c3 --prix 2600000   # fiche financière
python dispatch.py foncier scan           # ventes pour taxes des MRC (gratuit)
python dispatch.py foncier seao           # avis d'appel d'offres → signaux de secteur
python dispatch.py foncier marche         # alertes Centris → croisement avec la base
python dispatch.py foncier calibrer --fichier alerte.eml       # mise au point du parseur
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
| `0 11 * * 1-5` | jours ouvrables 7 h | `marche` — alertes Centris |
| `0 10 * * 2` | mardi 6 h | `scan` — ventes pour taxes |
| `0 10 * * 4` | jeudi 6 h | `seao` — nouveaux avis |
| `30 11 * * 1` | lundi 7 h 30 | `brief` — dossiers du jour |

`marche` est le seul scan quotidien : une mise en marché se joue en heures,
alors qu'un rôle d'évaluation bouge une fois l'an.

L'enrichissement payant n'est **jamais** automatique. Il se déclenche à la main.

---

## 4bis. Le côté « déjà à vendre » — alertes Centris

Le système a deux moitiés, comme la thèse (« Tjrs Centris / mls ») :

| | Off-market | Sur le marché |
|---|---|---|
| Commandes | `scan`, `seao`, `roles` | `marche` |
| Ce que ça détecte | ce qui *va* se vendre | ce qui *est* à vendre |
| Ton avantage | tu es seul à le voir | tu sais déjà tout sur le vendeur |

**Aucune extraction du site Centris.** Le chemin est l'inverse : tu configures
une alerte de recherche sauvegardée sur centris.ca avec tes critères, Centris
t'envoie les nouvelles fiches par courriel, et l'agent lit ces courriels via ton
compte Gmail déjà branché.

C'est meilleur en ingénierie, pas seulement en droit :

| Extraction directe | Alerte → Gmail |
|---|---|
| Tu tires la donnée | Centris te la pousse |
| Casse à chaque refonte HTML | Format courriel stable |
| Débit limité, blocage IP | Aucune limite |
| Manquement aux conditions | Service prévu pour ça |
| Filtrage par ton code | Filtrage par Centris, gratuit |

### Ce qui est conservé

Le lien et les **faits** : adresse, prix demandé, nombre de logements, numéro
Centris. Ni photo, ni texte descriptif — la fiche se consulte sur centris.ca en
cliquant le lien. Un fait n'est pas une œuvre protégée ; sa présentation l'est.

### Le moment qui vaut le plus cher

```
Un immeuble était à 72 points dans ta base depuis 8 mois.
Il apparaît sur Centris ce matin.

Tu sais déjà : proprio depuis 31 ans, hypothèque presque éteinte,
dans le PPU, terrain sous-utilisé, écart de +2 % sur la valeur au rôle.

Les autres découvrent une fiche. Toi, un dossier complet, le jour 1.
```

C'est ce que produit `marche` : le rapprochement entre une fiche fraîche et un
dossier suivi, via `core/foncier/adresse.py` qui réconcilie « 1450 ONTARIO E »
du rôle avec « 1450, rue Ontario Est » de Centris.

### Calibration obligatoire avant de s'y fier

Le format exact des courriels d'alerte n'a pas pu être observé à l'écriture du
parseur. Il emploie plusieurs stratégies et rapporte toujours ce qu'il a manqué.
Avant la première utilisation :

```bash
python dispatch.py foncier calibrer --fichier alerte-centris.eml
```

Le rapport affiche, fiche par fiche, ce qui a été extrait, ce qui est marqué
`❌ NON EXTRAIT`, et un extrait du texte réellement analysé. Les motifs
d'extraction sont en tête de `core/foncier/sources/centris.py` et s'ajustent en
quelques lignes.

### Statut plutôt que score

« Sur le marché » n'ajoute aucun point. Le score mesure la **motivation du
vendeur** ; qu'un immeuble soit listé n'en dit rien de plus — le scoring l'a
déjà évaluée. C'est un fait d'état, déclaré dans `criteres.SIGNAUX_INFORMATIFS`
et exempté du calcul.

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
| Scraper Centris / DuProprio | Voir ci-dessous. Le système passe par les alertes courriel (`marche`), qui donnent le même résultat sans le risque. |
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
code passe par des fournisseurs sous entente. Ne pas contourner.

**Extraction de Centris** — la question revient toujours : « je n'ai signé aucun
contrat, comment pourrais-je être en défaut ? » Trois réponses, dans l'ordre de
solidité.

1. **Le droit d'auteur ne demande aucun contrat.** Les photos et les textes des
   fiches sont des œuvres protégées. Les reproduire est une contrefaçon, point.
   C'est pourquoi ce système ne conserve **que des faits** — adresse, prix,
   nombre de logements, lien.
2. **Un contrat peut se former sans signature.** L'[article 1386 C.c.Q.](https://www.legisquebec.gouv.qc.ca/fr/version/lc/CCQ-1991?code=se:1386)
   admet le consentement tacite. Dans [*Century 21 c. Rogers (Zoocasa)*, 2011 BCSC 1196](https://canliiconnects.org/en/summaries/31571),
   un tribunal canadien a jugé qu'un « browsewrap » — de simples conditions liées
   en bas de page, sans clic — liait un extracteur de fiches immobilières :
   1 000 $ pour le contrat, 32 000 $ pour le droit d'auteur, injonction
   permanente. L'[article 1435 C.c.Q.](https://www.legisquebec.gouv.qc.ca/fr/version/lc/CCQ-1991?code=se%3A1435)
   offre une meilleure protection au Québec, sauf s'il est prouvé qu'on avait
   connaissance des conditions — ce qu'un extracteur ciblé rend difficile à nier.
3. **Filtrer exige d'extraire.** « Mon outil ne garde que le lien » décrit ce
   qu'il conserve, pas ce qu'il fait : il a dû lire chaque fiche pour décider
   laquelle retenir.

Le risque concret n'est d'ailleurs pas le tribunal — c'est le blocage IP, la
perte du compte, et une architecture qui casse à chaque refonte du site.
`marche` obtient le même résultat par la porte d'en avant.

**Loi 25** — le nom d'un propriétaire physique est un renseignement personnel.
La [Commission d'accès à l'information][cai] est explicite : la prospection
commerciale **n'est pas une « fin compatible »** et exige un consentement
préalable. Acheter pour son propre compte et écrire à un propriétaire est très
différent de constituer puis revendre une base de leads — le second expose
sérieusement. Pour les courriels, la LCAP s'ajoute.

**OACIQ** — acheter pour soi ne demande aucun permis. Mettre en relation
acheteur et vendeur contre rémunération, c'est du courtage : permis obligatoire.

---

## 7bis. Analyse de transaction

Le moteur répond à « lequel regarder ». `core/foncier/montage.py` répond à
« combien offrir, et est-ce que ça tient debout ».

Trois calculs décident de tout :

| | Ce que ça dit |
|---|---|
| **Prix pour un cap de 7-8 %** | ce que tu peux payer selon ta thèse |
| **Plafond du prêteur (DSCR 1,20)** | ce que la banque acceptera de financer — souvent plus contraignant |
| **Cashflow** | ce qui reste après la dette ; un immeuble à 7 % financé à 5,75 % laisse peu |

Quatre scénarios de financement sont comparés automatiquement (25 %, 15 %, 35 %,
amorti 30 ans), plus une **analyse de sensibilité** : que devient le dossier si
le NOI réel est 15 % sous l'estimation ? C'est la question qui compte quand le
NOI vient du rôle et non des états financiers.

```bash
python dispatch.py foncier analyse --id a1b2c3 --prix 2600000
python dispatch.py foncier analyse --id a1b2c3 --revenus 280000 --depenses 110000
```

Dans l'interface, le calculateur est dans le panneau du dossier et se recalcule
en direct.

**Les droits de mutation utilisent le barème de base du Québec.** Montréal
applique des tranches supérieures au-delà de 500 000 $ : sur un immeuble de
plusieurs millions, la « taxe de bienvenue » réelle est plus élevée que celle
affichée. La fiche le rappelle chaque fois que la municipalité est Montréal.

---

## 7ter. Suivi et relances

`core/foncier/suivi.py` porte ce que le moteur ne peut pas savoir : à qui tu as
parlé, ce qui s'est dit, et quand relancer.

Après chaque contact journalisé, **la relance se programme toute seule** selon le
résultat :

| Résultat | Relance |
|---|---|
| visite planifiée | 3 jours |
| intéressé | 5 jours |
| offre déposée | 5 jours |
| à rappeler | 7 jours |
| états financiers demandés | 10 jours |
| sans réponse | 14 jours |
| refus | 180 jours — un refus n'est pas éternel |

Un refus renvoie le dossier en **surveillance**, pas à la poubelle : le
propriétaire qui dit non en juillet peut dire oui après une succession.

`foncier aujourdhui` agrège tout ce qui mérite une action — mises en marché,
signaux de détresse, scores en hausse, relances en retard — trié par urgence,
un dossier n'apparaissant qu'une fois.

---

## 8. Vérification

```bash
python scripts/test_foncier.py
```

188 vérifications sur données synthétiques, sans réseau ni clé d'API : filtres
de la thèse, scoring, estimations financières, géométrie, tolérance de schéma
des rôles, extraction des avis de vente pour taxes, normalisation d'adresses,
parseur d'alertes Centris, croisement marché × base, montage financier, CRM,
registre des MRC, persistance et garde-fous budgétaires.

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
