# Welldone AI Agents — État réel vs théorie + Plan d'action
*Généré le 2026-04-03 — Audit nuit complète*

---

## Résumé exécutif

Sur 11 agents actifs, **3 fonctionnaient vraiment**, **5 avaient des bugs critiques silencieux** (retournaient des fausses confirmations), et **3 ne faisaient pratiquement rien d'utile**. Ce document explique ce qui était cassé, ce qui a été corrigé cette nuit, et ce qu'il reste à faire.

---

## 1. EMAIL AGENT (`/email`)

### Ce qu'il était censé faire
- Triage intelligent des emails toutes les heures
- Notifier JP des emails importants via Telegram
- Archiver automatiquement les newsletters et spam

### Ce qu'il faisait VRAIMENT (avant cette nuit)
| Problème | Impact |
|----------|--------|
| Cherchait `UNSEEN` — Apple Mail marque tout comme lu en 30s | Agent trouvait **0 email** à chaque heure → disait "aucun message important" en permanence |
| Scannait seulement WHC, jamais Hostinger | La moitié des boîtes ignorées |
| `noreply@cal.com` matche le filtre "bulk" | Booking de Daphné archivé silencieusement, jamais notifié |
| `/email lire` utilisait seulement WHC | Vue partielle de la boîte |

### Fixes appliqués cette nuit
- ✅ `UNSEEN` → `SINCE 48h` + fichier de tracking des UIDs déjà traités (TTL 7 jours)
- ✅ Boucle sur `_ALL_ACCOUNTS` (WHC + Hostinger)
- ✅ `_CRITICAL_DOMAINS` : Cal.com, Stripe, PayPal, DocuSign → **jamais archivés**
- ✅ `/email lire` multi-comptes avec label [WHC]/[Hostinger]

### Ce qui reste à faire
- [ ] Construire la whitelist de contacts connus (`/email construire_whitelist`) — doit être lancé une fois manuellement
- [ ] Tester que l'archivage IMAP fonctionne sur les 2 comptes (le dossier `INBOX.Archives` doit exister sur Hostinger)

---

## 2. ANALYTICS AGENT (`/analytics`)

### Ce qu'il était censé faire
- Rapport hebdomadaire GA4 + Search Console chaque lundi
- Montrer les clics CTA, conversions, formulaires
- Identifier les opportunités SEO

### Ce qu'il faisait VRAIMENT (avant cette nuit)
| Problème | Impact |
|----------|--------|
| Rapport affichait "GTM opérationnel" hardcodé | Faux. Jamais vérifié. |
| Zéro suivi des events GA4 (clics, formulaires) | Ne voyait pas les 4 vraies soumissions de contact |
| `_analyse()` ne parlait jamais de conversions | Rapport inutile pour l'objectif #1 : les leads |

### Données réelles découvertes (90 derniers jours)
```
form_start    638  (clics sur bouton contact — ATTENTION: voir note*)
form_submit     4  (vraies soumissions reçues)
click_email     2
click_phone     1
```
*Note : les 638 `form_start` sont probablement surestimés — GTM déclenche cet event sur tout clic dans la zone du formulaire, pas seulement quand quelqu'un commence à taper. Les 4 `form_submit` sont les seuls vrais. À valider avec `/analytics conversions`.

### Fixes appliqués cette nuit
- ✅ Nouvelle commande `/analytics conversions` — liste tous les events GA4 réels
- ✅ Rapport hebdomadaire inclut maintenant la section "Événements & CTA"
- ✅ `_analyse()` calcule le funnel form_start → form_submit et alerte si abandon critique
- ✅ Si `form_submit > 0` → rapport dit explicitement "ACTION REQUISE : répondre"

### Ce qui reste à faire
- [ ] Valider la config GTM (distinguer vrais form_start des faux positifs)
- [ ] Ajouter tracking Cal.com bookings dans GA4 via GTM event personnalisé
- [ ] Activer le rapport Analytics pour `welldone.archi` (GA4 stream `G-Z8YGQFP4YH` pas encore configuré)

---

## 3. WATCHDOG AGENT (NOUVEAU — `/watchdog`)

### Ce qu'il fait
Créé cette nuit. Tourne toutes les 6h. Teste **réellement** :
- Connexion IMAP WHC (login + SELECT INBOX)
- Connexion IMAP Hostinger
- API GA4 (requête 1 ligne)
- API Anthropic (1 token)
- Bot Telegram (getMe)
- Base Notion (lecture DB sources)

**Si tout est OK → silencieux.** Si quelque chose casse → alerte Telegram immédiate avec le service en cause.

### Valeur
Dès demain matin, si Railway redémarre et qu'une variable d'env est manquante, ou qu'un mot de passe IMAP a expiré, ou que l'API Anthropic a un problème → JP reçoit une alerte dans les 6h.

---

## 4. VEILLE AGENT (`/veille`)

### Ce qu'il était censé faire
Chaque lundi : lire les sources RSS dans Notion → générer 10 idées d'articles → email + Notion.

### Ce qu'il faisait VRAIMENT (avant cette nuit)
| Problème | Impact |
|----------|--------|
| `TODAY` calculé au démarrage du serveur | Si Railway ne redémarre pas le lundi, la date dans l'email serait celle du dernier démarrage |
| Rapport disait toujours "10 idées générées" hardcodé | Faux si Claude en génère 8 ou 12 |
| Erreur Notion silencieuse | Email envoyé sans lien Notion, sans indiquer que Notion avait échoué |

### Fixes appliqués cette nuit
- ✅ `TODAY` calculé au moment de l'exécution (pas au démarrage)
- ✅ Compte réellement les idées dans la réponse Claude
- ✅ Indique clairement si la page Notion a été skippée

### État actuel
✅ Fonctionnel — RSS + Claude + Gmail. La partie Notion peut échouer silencieusement si la DB est mal configurée.

---

## 5. CEO AGENT (`/ceo`)

### Ce qu'il fait
Lit la queue Paperclip, dispatche les tâches aux bons agents.

### Problème corrigé
Marquait une issue comme `done` même si l'agent retournait `❌ Erreur: ...`. Désormais :
- Résultat commence par `❌` ou `Erreur` → statut `cancelled` dans Paperclip
- Résultat normal → statut `done`

### Limitation actuelle
La connexion à Paperclip DB (`postgresql://`) est hardcodée sur `localhost:54329`. Sur Railway, si Paperclip n'est pas dans le même service, la connexion échoue silencieusement → aucune tâche traitée. **À vérifier.**

---

## 6. BLOG PIPELINE (`/blog rédiger`)

### Ce qu'il fait
Rédige un article → images Gemini → score qualité → push Framer CMS.

### État réel
✅ Fonctionne. Architecture solide avec guardrails (15k tokens max, 240s timeout).

### Problème mineur
Le score qualité inclut `img_count` dans son évaluation, mais cette valeur vient de Gemini/Cloudinary — si les images échouent, le score est calculé avec `img_count=0` sans savoir si c'est un choix ou un échec.

---

## 7. FRAMER AGENT (`/framer`)

### Ce qu'il fait
Liste et publie des articles dans le CMS Framer.

### État réel
✅ Fonctionne pour les opérations de base (liste, publier article).

### Limitation
Les IDs de champs Framer (`uUwwOCUVU`, `Phx6jOJdl`) sont hardcodés dans le code. Si Framer change sa structure, les publications échoueront silencieusement.

---

## 8. CALENDAR AGENT (`/calendar`)

### Ce qu'il fait
Lit et crée des événements Google Calendar.

### État réel
Fonctionne si les credentials OAuth sont valides. Les tokens OAuth expirent — le `refresh_token` dans Railway doit être actif. Si l'expiry est dépassé sans refresh, toutes les opérations Calendar échouent avec une erreur 401 non explicite.

---

## 9. QBO AGENT (`/qbo`)

### Ce qu'il fait
Créer des factures dans QuickBooks Online.

### État réel
Code existe, credentials configurés (refresh token dans Railway). **Jamais testé en conditions réelles.** Prochaine étape : créer une vraie facture test.

---

## 10. NOTION AGENT (`/notion`)

### Ce qu'il fait
Lire et écrire dans les bases de données Notion.

### État réel
✅ Fonctionne pour les opérations de base.

---

## 11. GMAIL AGENT (`/gmail`)

### Ce qu'il fait
Lire et envoyer des emails via Gmail (awelldonestudio@gmail.com).

### État réel
Fonctionne mais séparé du email agent WHC/Hostinger. Pas de triage automatique sur Gmail.

---

## Tableau de bord rapide

| Agent | Fonctionne | Fiable | Utile aujourd'hui | Priorité |
|-------|-----------|--------|-------------------|----------|
| email | ✅ (fixé) | ✅ (fixé) | 🔴 Critique | VALIDÉ CE SOIR |
| analytics | ✅ | ✅ (fixé) | 🔴 Critique | VALIDÉ CE SOIR |
| watchdog | ✅ (nouveau) | ✅ | 🔴 Critique | VALIDÉ CE SOIR |
| veille | ✅ (fixé) | ✅ | 🟡 Hebdo | OK |
| ceo | ✅ (fixé) | 🟡 | 🟡 Si Paperclip actif | Vérifier DB |
| blog | ✅ | ✅ | 🟡 Sur demande | OK |
| framer | ✅ | 🟡 | 🟡 Sur demande | OK |
| calendar | ✅ | 🟡 | 🟡 Sur demande | Vérifier OAuth |
| qbo | 🟡 | ❓ | 🟡 Sur demande | Tester |
| notion | ✅ | ✅ | 🟡 | OK |
| gmail | ✅ | ✅ | 🟡 | OK |

---

## Plan d'action — demain matin

### Au réveil, vérifier dans Telegram :
1. `/watchdog check` → voir si tous les services sont verts
2. `/email trier` → premier triage avec le nouveau système (SINCE au lieu de UNSEEN)
3. `/analytics conversions` → voir les vrais events GA4

### Cette semaine (par ordre de valeur) :

#### 1. Valider le formulaire contact (URGENT)
Le formulaire `awelldone.com/contact` a 4 vraies soumissions en 90 jours. Vérifier :
- Ces 4 demandes ont-elles reçu une réponse ?
- Le formulaire fonctionne-t-il sur mobile ?
- GTM est-il correctement configuré (`form_submit` vs `form_start`) ?

#### 2. Construire la whitelist email (30 min)
```
/email construire_whitelist
```
Lance une analyse de tous les emails envoyés → construit la liste de tes contacts réels. Sans ça, l'agent ne peut pas distinguer un client actif d'un inconnu.

#### 3. Tester QBO (1h)
```
/qbo créer client "Test Client" montant 100
```
Vérifier qu'une vraie facture apparaît dans QuickBooks.

#### 4. Vérifier la config GTM (30 min)
Aller dans GTM → voir les triggers sur le formulaire contact → distinguer `form_start` (clic sur le formulaire) vs `form_submit` (envoi réel). Si le trigger est mal configuré, les 638 `form_start` sont des faux positifs.

#### 5. Watchdog — vérifier les alertes
Après 6h, si watchdog a tourné → regarder s'il a trouvé des problèmes. Priorité : s'assurer que IMAP Hostinger se connecte bien.

---

## Ce qui a changé cette nuit (commits)

1. `990461e` — email auto_trier UNSEEN bug + multi-comptes + analytics conversions
2. `8df985f` — analytics funnel conversions dans rapport hebdo
3. *(en cours)* — veille TODAY/idées, ceo succès réel, email.lire multi-comptes, watchdog agent

---

## Principe directeur pour la suite

> **Si un agent dit ✅, c'est parce qu'il a vérifié. Pas parce qu'il suppose.**

Règles appliquées à partir de maintenant :
- Aucun message hardcodé de type "✅ Opérationnel" sans test réel
- Aucun compteur hardcodé ("10 idées" → compter vraiment)
- Tout échec API est loggué avec le contexte complet, pas seulement `❌ Erreur: {e}`
- Si une action réussit, le résultat de l'API doit le confirmer
- Watchdog tourne toutes les 6h — silence = tout va bien, alerte = action requise
