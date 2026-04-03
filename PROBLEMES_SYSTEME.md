# PROBLÈMES SYSTÈME — État honnête
## Welldone AI OS — Ce qui est cassé, incomplet ou inutile
**Mis à jour :** 2026-04-03
**Principe :** aucune déclaration sans preuve. Chaque problème listé est documenté.

---

## 🔴 PROBLÈMES CRITIQUES — le système ne fonctionne pas

### 1. IMAP WHC — `service not known` depuis Railway
**Agent concerné :** email (auto_trier)
**But premier de l'agent :** scanner ta boîte et t'alerter quand un email important arrive
**Ce qu'il fait réellement :** plante au démarrage avec `service not known` → ne scanne rien → ne notifie rien
**Pourquoi :** `mail.awelldone.com` ne se résout pas en DNS depuis les serveurs Railway. La variable `WHC_IMAP_HOST` est probablement absente ou incorrecte dans Railway.
**Impact :** 0 notification email. Tout ce qui suit dans cette section email est théorique tant que ce n'est pas réglé.
**Fix requis :** Railway → Variables → `WHC_IMAP_HOST` = hostname IMAP exact depuis cPanel WHC

---

### 2. Le CTA du site ouvre `mailto:` — aucune notification possible en temps réel
**Agent concerné :** email
**But premier :** te notifier quand un client potentiel contacte via le site
**Ce qu'il fait réellement :** rien jusqu'à ce que l'email arrive dans ta boîte IMAP et que l'agent le scanne
**Pourquoi c'est sous-optimal :**
- Délai minimum : 5 minutes (scan cron) — même quand l'IMAP fonctionne
- `mailto:` ne fonctionne pas bien sur mobile (ouvre l'app mail, beaucoup abandonnent)
- Aucune structure : pas de nom, pas de numéro, sujet libre
- Pas de notification si l'IMAP est down (comme maintenant)
- Taux de conversion 3-5x inférieur à un formulaire intégré
**Fix requis :** Remplacer le bouton `mailto:` par un formulaire Framer → webhook `/webhook/form` déjà construit → notification Telegram en < 2 secondes

---

## 🟠 PROBLÈMES MAJEURS — le système tourne mais produit des résultats faux ou inutiles

### 3. Rapport analytics — "GTM opérationnel" était du flan pur
**Agent concerné :** analytics
**But premier :** te dire si le tracking fonctionne, quels boutons sont cliqués, d'où vient ton trafic
**Ce qu'il faisait réellement (avant fix) :** affichait `"✅ GTM opérationnel — tracking clics & scroll actif"` dans chaque rapport. Cette ligne était une **string hardcodée dans le code**. Elle ne vérifiait rien. Elle s'affichait même si GTM était désactivé, même si aucun clic n'était jamais tracké.
**Ce qu'il fait maintenant (après fix) :** interroge GA4 sur la dimension `eventName`. Mais si les tags GTM ne sont pas publiés ou mal configurés, le rapport affichera simplement "aucun événement" — ce qui est au moins honnête.
**Problème résiduel :** les tags GTM n'ont pas été validés. Le rapport de ce matin avait **0 tokens utilisés** — soit le rapport a tourné avant le déploiement du fix, soit la connexion GA4 échoue silencieusement.
**Fix requis :** vérifier dans GA4 → Temps réel → Événements si des événements remontent quand tu navigues sur le site

---

### 4. Rapport analytics hebdo — "Tache terminee: analytics.rapport — Tokens utilisés: 0/15,000"
**Agent concerné :** analytics (rapport de ce matin)
**Ce que ça veut dire :** l'agent a tourné et s'est terminé sans utiliser aucun token LLM. Soit il a retourné une erreur silencieuse très tôt, soit il a utilisé un cache.
**Pourquoi c'est un problème :** un rapport analytique qui utilise 0 token d'IA n'a pas analysé quoi que ce soit. Le rapport que tu as reçu ce matin a probablement été généré avant notre déploiement — il ne reflète pas les corrections apportées.
**Fix requis :** relancer manuellement `/analytics rapport` pour voir la version corrigée

---

### 5. Email — notifications "aucun message" toutes les heures (comportement corrigé mais pattern à comprendre)
**Agent concerné :** email (auto_trier)
**But premier :** t'alerter quand un email important arrive
**Ce qu'il faisait :** tournait chaque heure, trouvait 0 email (à cause du bug UNSEEN), et **ne te disait rien** — ni "aucun email" ni "j'ai trouvé quelque chose". Le message "aucun message à trier" allait uniquement dans les logs Railway.
**Ce qu'il fait maintenant :** scan toutes les 5 min, SINCE 48h, UID tracking. Notifie **seulement** quand il trouve quelque chose. Silence si rien.
**Problème résiduel :** inutile tant que `service not known` n'est pas réglé (#1 ci-dessus)

---

### 6. Email — un seul compte scanné sur deux
**Agent concerné :** email
**But premier :** couvrir toutes tes boîtes
**Ce qu'il faisait :** scannait uniquement WHC (`jptanguay@awelldone.com`). Hostinger ignoré.
**Ce qu'il fait maintenant :** boucle sur `_ALL_ACCOUNTS` (WHC + Hostinger)
**Problème résiduel :** si les variables `HST_IMAP_HOST`, `HST_EMAIL`, `HST_PASSWORD` ne sont pas dans Railway, Hostinger échoue silencieusement aussi

---

### 7. Voyage — les prix affichés sont fictifs
**Agent concerné :** voyage
**But premier :** trouver les vraies meilleures offres de vols
**Ce qu'il fait réellement :** connecte au **sandbox Amadeus** (`test.api.amadeus.com`) par défaut. Les prix, vols, horaires affichés sont des **données de test inventées par Amadeus** — pas des vrais vols.
**Ce qu'il fait maintenant :** affiche `⚠️ [SANDBOX — DONNÉES FICTIVES]` en tête de résultat
**Fix requis :** Railway → Variables → `AMADEUS_BASE_URL = https://api.amadeus.com`

---

### 8. QBO — jamais testé en conditions réelles
**Agent concerné :** qbo
**But premier :** créer et envoyer des factures QuickBooks
**Ce qu'il fait réellement :** inconnu — personne n'a jamais créé une vraie facture avec cet agent depuis Railway.
**Bugs corrigés :** TaxCode (`is False` → `!= False`), fallback numéro de facture non-unique
**Problème résiduel :** le token OAuth QBO expire. Si `QBO_REFRESH_TOKEN` n'a pas été renouvelé depuis le déploiement initial, toutes les opérations QBO échouent avec une 401 silencieuse.
**Fix requis :** tester une vraie facture + vérifier les logs Railway pour voir si l'auth QBO fonctionne

---

## 🟡 PROBLÈMES MINEURS — le système tourne, l'information est partiellement correcte

### 9. Veille — date figée si Railway tourne plusieurs jours sans restart (corrigé)
**Agent concerné :** veille
**Ce qu'il faisait :** `TODAY = datetime.today()` calculé **au démarrage du serveur Railway**. Si Railway tourne 5 jours sans redémarrer, le rapport du mercredi dit "Mardi" dans le titre.
**Statut :** corrigé — `TODAY` est maintenant recalculé à chaque exécution de `run()`

---

### 10. Veille — "💡 10 idées générées" était hardcodé (corrigé)
**Agent concerné :** veille
**Ce qu'il faisait :** affichait toujours "10 idées générées" dans le résumé, peu importe ce que Claude avait réellement produit. Si Claude renvoyait 3 idées ou une erreur, le message disait quand même "10".
**Statut :** corrigé — regex compte les lignes numérotées réelles

---

### 11. Blog pipeline — nombre d'images inventé (corrigé)
**Agent concerné :** blog_pipeline
**Ce qu'il faisait :** comptait le nombre de fois que le mot "image" ou "photo" apparaissait dans la réponse texte de Claude. Un message d'erreur comme "impossible de générer les images" pouvait être compté comme 3 images.
**Statut :** corrigé — regex extrait le chiffre explicite

---

### 12. Layout Guardian — analysait les messages d'erreur HTTP comme du HTML (corrigé)
**Agent concerné :** layout_guardian
**Ce qu'il faisait :** si la page Framer était inaccessible, la fonction de fetch retournait une string d'erreur `[Impossible de fetch...]`. Cette string était passée à Claude **comme si c'était le HTML de la page**. Claude produisait un rapport de "problèmes de layout" basé sur un message d'erreur.
**Statut :** corrigé — guard avant appel Claude. Retourne une erreur claire si fetch échoue.
**Problème résiduel :** dépend de `FRAMER_STAGING_URL` configurée dans Railway. Si non configurée, l'agent ne peut rien faire.

---

### 13. CEO — tâches échouées marquées "done" dans Paperclip (corrigé)
**Agent concerné :** ceo
**Ce qu'il faisait :** peu importe le résultat d'un agent (succès, erreur, timeout), la tâche Paperclip passait à `"done"`. Tu voyais des tâches "terminées" dont l'exécution avait en réalité planté.
**Statut :** corrigé — vérifie si le résultat commence par `❌` → passe à `cancelled`

---

### 14. Gmail — exceptions silencieuses sur `search_contact` (corrigé)
**Agent concerné :** gmail
**Ce qu'il faisait :** `search_contact` retournait "aucun contact trouvé" sans jamais logger pourquoi — que l'API soit désactivée, les scopes manquants, ou l'email introuvable. Deux `except Exception: pass` avalaient tout.
**Statut :** corrigé — `log.warning()` dans les deux blocs

---

### 15. Dispatcher — agents en échec de chargement invisibles (corrigé)
**Agent concerné :** core/dispatcher
**Ce qu'il faisait :** si un agent plantait à l'import (variable manquante, module absent), il était silencieusement absent du système. Tu pouvais envoyer une commande à cet agent et recevoir "agent inconnu" sans comprendre pourquoi.
**Statut :** corrigé — `FAILED_AGENTS` dict, visible dans `/help` et dans les logs

---

### 16. Config — clé API Framer exposée dans le code source versionné (corrigé)
**Fichier :** config.py
**Ce qu'il y avait :** `FRAMER_API_KEY = os.environ.get("FRAMER_API_KEY", "fr_2xsx07vykt81c9y2p0krj2xgmk")` — la vraie clé était la valeur par défaut, donc présente dans le code GitHub.
**Statut :** corrigé — valeur par défaut supprimée

---

## 📋 TABLEAU RÉCAPITULATIF

| Agent | But premier | Fonctionne ? | Fiable ? | Problème principal |
|---|---|---|---|---|
| **email** | Alerter sur emails importants | ❌ | ❌ | IMAP `service not known` dans Railway |
| **analytics** | Mesurer trafic + CTA | ⚠️ | ⚠️ | Rapport de ce matin = 0 tokens. GTM non validé. |
| **blog_pipeline** | Rédiger + publier articles | ✅ | ✅ | — |
| **ceo** | Dispatcher les tâches Paperclip | ✅ | ✅ | — |
| **framer** | Publier dans le CMS | ✅ | ✅ | — |
| **gmail** | Lire/envoyer via Gmail | ✅ | ✅ | — |
| **layout_guardian** | Détecter problèmes UI | ⚠️ | ✅ | Dépend de FRAMER_STAGING_URL |
| **notion** | Créer tâches / pages | ✅ | ✅ | — |
| **qbo** | Facturer via QuickBooks | ⚠️ | ❓ | Jamais testé en prod. Token OAuth potentiellement expiré. |
| **veille** | Veille hebdo → idées articles | ✅ | ✅ | — |
| **voyage** | Chercher des vols | ⚠️ | ❌ | Données sandbox = fictives tant que AMADEUS_BASE_URL non changé |
| **watchdog** | Surveiller tous les services | ✅ | ⚠️ | Nouveau — pas encore prouvé en production |
| **calendar** | Créer/lire événements Google | ❓ | ❓ | Jamais utilisé / non testé |
| **qualite** | Scorer la qualité d'un article | ❓ | ❓ | Agent interne pipeline uniquement |

---

## ✅ ACTIONS RESTANTES POUR TOI (dans l'ordre de priorité)

| # | Action | Où | Temps |
|---|---|---|---|
| 1 | Trouver et corriger `WHC_IMAP_HOST` dans Railway | Railway + cPanel WHC | 5 min |
| 2 | Remplacer le bouton `mailto:` par un formulaire Framer | Framer | 15 min |
| 3 | Configurer `AMADEUS_BASE_URL=https://api.amadeus.com` | Railway | 2 min |
| 4 | Relancer `/analytics rapport` pour voir la version corrigée | Telegram | 1 min |
| 5 | Tester une vraie facture QBO pour valider l'auth OAuth | Telegram | 10 min |
| 6 | Vérifier `HST_IMAP_HOST`, `HST_EMAIL`, `HST_PASSWORD` dans Railway | Railway | 5 min |
