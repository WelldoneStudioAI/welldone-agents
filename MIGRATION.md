# Guide de migration — Welldone AI Agent Team

## Vue d'ensemble

Tu migres de 5 scripts éparpillés + GitHub Actions → 1 service Railway unifié.
Les anciens fichiers sont conservés jusqu'à validation complète.

---

## Étape 1 — Service Account Google (GA4 + Search Console)

> **Pourquoi ?** Le Service Account ne s'expire jamais. Fin des tokens base64 cassés.

### 1a. Créer le Service Account

1. Aller sur [Google Cloud Console](https://console.cloud.google.com)
2. Sélectionner ton projet → **IAM & Admin** → **Service Accounts**
3. **+ Créer un compte de service**
   - Nom: `welldone-ai-agent`
   - Rôle: aucun (les accès se donnent directement dans GA4 et GSC)
4. Cliquer sur le compte créé → **Clés** → **Ajouter une clé** → JSON
5. Télécharger le fichier `*.json`

### 1b. Donner accès à GA4

1. Aller dans [GA4 Admin](https://analytics.google.com) → Propriété → **Gestion des accès**
2. Ajouter l'email du service account (format: `name@project.iam.gserviceaccount.com`)
3. Rôle: **Lecteur**

### 1c. Donner accès à Search Console

1. Aller dans [Google Search Console](https://search.google.com/search-console)
2. **Paramètres** → **Utilisateurs et autorisations** → Ajouter un utilisateur
3. Email du service account → Accès: **Propriétaire restreint**
4. Répéter pour chaque site (awelldone.com, welldone.archi)

### 1d. Encoder et stocker

```bash
# Encoder en base64 (copie dans le presse-papier)
bash scripts/encode_service_account.sh path/to/service_account.json

# Coller dans Railway → GOOGLE_SA_JSON_B64
```

---

## Étape 2 — Token OAuth Google (Gmail + Calendar)

> **Pourquoi ?** Un seul token avec refresh automatique, plus de doublon entre GitHub et Railway.

### 2a. Vérifier que les APIs sont activées dans Google Cloud Console

- Gmail API
- Google Calendar API
- People API (Contacts)

### 2b. Créer ou réutiliser un OAuth 2.0 Client ID

1. Google Cloud Console → **Credentials** → **+ Create Credentials** → **OAuth client ID**
2. Type: **Desktop app**
3. Télécharger le `client_secret.json`

### 2c. Générer le token une fois localement

```bash
# Dans le dossier welldone-agents
pip install -r requirements.txt
python scripts/generate_oauth_token.py --client-secret path/to/client_secret.json
```

Le navigateur s'ouvre → connecte-toi avec `jptanguay@awelldone.studio` → accepte tout.

```bash
# Le token est sauvegardé dans oauth_token.json
# Copier le contenu JSON brut dans Railway → GOOGLE_OAUTH_JSON
cat oauth_token.json | pbcopy
```

---

## Étape 3 — Refresh Token Zoho

> Si tu as déjà un ZOHO_TOKEN_JSON_B64 qui fonctionne, extraire juste le refresh_token.

### Option A — Extraire depuis le token existant

```bash
# Décoder le token actuel
echo "COLLE_TON_ZOHO_TOKEN_JSON_B64_ICI" | base64 -d | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('refresh_token:', data.get('refresh_token', 'NON TROUVÉ'))
"
```

### Option B — Générer un nouveau token

```bash
python scripts/generate_zoho_token.py
```

---

## Étape 4 — Configurer Railway

### 4a. Mettre à jour les variables

```bash
# Remplir .env depuis .env.example
cp .env.example .env
# Éditer .env avec toutes les valeurs

# Vérifier ce qui sera appliqué (dry run)
bash scripts/setup_railway.sh

# Appliquer sur Railway (Railway CLI requis: brew install railway)
railway login
bash scripts/setup_railway.sh --apply
```

### 4b. Variables à supprimer de Railway (plus nécessaires)

- `GOOGLE_TOKEN_JSON_B64` → remplacé par `GOOGLE_SA_JSON_B64` + `GOOGLE_OAUTH_JSON`
- `ZOHO_TOKEN_JSON_B64` → remplacé par `ZOHO_REFRESH_TOKEN`
- `GH_PAT` → plus nécessaire (GitHub Actions crons supprimés)
- Tout ce qui était lié aux GitHub Actions workflows

---

## Étape 5 — Tester en local

```bash
# Vérifier tous les tokens
python health.py

# Tester un agent spécifique
python dispatch.py gmail read
python dispatch.py analytics sources
python dispatch.py zoho list

# Démarrer le bot complet
python main.py
```

---

## Étape 6 — Déployer sur Railway

```bash
# Vérifier que les nouveaux fichiers sont trackés
git status

# Commit
git add .
git commit -m "refactor: modular agent architecture v2"

# Push → GitHub Actions déclenche le redeploy automatiquement
git push origin main
```

---

## Étape 7 — Validation finale

Une fois Railway redéployé, envoyer sur Telegram :

```
/health        → tous les services verts ?
/gmail read    → emails non lus ?
/analytics sources → GA4 répond ?
/veille run    → lancer manuellement pour tester
```

---

## Après validation — Nettoyage

Une fois tout validé, supprimer les anciens fichiers :

```bash
rm bot.py analytics.py search_console.py email_rapport.py veille_lundi.py
rm .github/workflows/veille.yml.disabled
rm .github/workflows/rapport.yml.disabled
git add . && git commit -m "cleanup: remove legacy files"
```

---

## Troubleshooting rapide

| Problème | Cause probable | Solution |
|----------|----------------|----------|
| `GOOGLE_SA_JSON_B64 non défini` | Variable manquante sur Railway | `railway variables set GOOGLE_SA_JSON_B64=...` |
| `Token OAuth invalide` | GOOGLE_OAUTH_JSON mal copié | Re-générer avec `generate_oauth_token.py` |
| `Zoho refresh failed` | ZOHO_REFRESH_TOKEN expiré ou manquant | Re-générer avec `generate_zoho_token.py` |
| Bot Telegram ne répond pas | Railway crash au démarrage | `railway logs --tail` pour voir l'erreur |
| Crons ne s'exécutent pas | APScheduler pas démarré | Vérifier les logs Railway au boot |
