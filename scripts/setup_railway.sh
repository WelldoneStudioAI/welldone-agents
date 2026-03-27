#!/bin/bash
# setup_railway.sh — Configure toutes les variables Railway depuis .env
#
# Prérequis: Railway CLI installé (brew install railway) + railway login
# Usage:
#   bash scripts/setup_railway.sh           → affiche ce qu'il ferait
#   bash scripts/setup_railway.sh --apply   → applique réellement

APPLY=false
if [ "$1" == "--apply" ]; then
  APPLY=true
fi

# Vérifier que .env existe
if [ ! -f ".env" ]; then
  echo "❌ Fichier .env introuvable. Copie .env.example en .env et remplis les valeurs."
  exit 1
fi

# Variables à migrer (dans l'ordre)
VARS=(
  "TELEGRAM_BOT_TOKEN"
  "TELEGRAM_ALLOWED_USER_ID"
  "ANTHROPIC_API_KEY"
  "GOOGLE_SA_JSON_B64"
  "GOOGLE_OAUTH_JSON"
  "NOTION_TOKEN"
  "NOTION_TASK_DB"
  "NOTION_SOURCES_DB"
  "ZOHO_CLIENT_ID"
  "ZOHO_CLIENT_SECRET"
  "ZOHO_ORG_ID"
  "ZOHO_REFRESH_TOKEN"
  "GMAIL_RECIPIENT"
  "GA4_PROPERTY_ID"
)

echo ""
echo "═══════════════════════════════════════════════════════"
if $APPLY; then
  echo "  RAILWAY — Application des variables (--apply)"
else
  echo "  RAILWAY — Aperçu des variables (dry run)"
  echo "  → Utilise --apply pour appliquer réellement"
fi
echo "═══════════════════════════════════════════════════════"
echo ""

OK=0
MISSING=0

for VAR in "${VARS[@]}"; do
  # Lire la valeur depuis .env (ignore les commentaires et lignes vides)
  VALUE=$(grep "^${VAR}=" .env | cut -d'=' -f2- | sed 's/^"//' | sed 's/"$//')

  if [ -z "$VALUE" ]; then
    echo "  ⚠️  MANQUANT: $VAR"
    MISSING=$((MISSING + 1))
  else
    # Masquer les valeurs sensibles dans l'affichage
    DISPLAY="${VALUE:0:8}..."
    echo "  ✅ $VAR = $DISPLAY"
    if $APPLY; then
      railway variables set "$VAR=$VALUE" 2>/dev/null || echo "     ❌ Erreur Railway pour $VAR"
    fi
    OK=$((OK + 1))
  fi
done

echo ""
echo "─────────────────────────────────────────────────────"
echo "  $OK variables prêtes · $MISSING manquantes"
echo ""

if [ $MISSING -gt 0 ]; then
  echo "  ⚠️  Remplis les valeurs manquantes dans .env avant de continuer."
  echo ""
fi

if ! $APPLY; then
  echo "  → Lance avec --apply pour configurer Railway:"
  echo "    bash scripts/setup_railway.sh --apply"
  echo ""
fi
