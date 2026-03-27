#!/bin/bash
# encode_service_account.sh — Encode le JSON du Service Account en base64
# et l'affiche prêt à coller dans Railway GOOGLE_SA_JSON_B64
#
# Usage: bash scripts/encode_service_account.sh path/to/service_account.json

if [ -z "$1" ]; then
  echo "Usage: bash scripts/encode_service_account.sh path/to/service_account.json"
  exit 1
fi

if [ ! -f "$1" ]; then
  echo "❌ Fichier introuvable: $1"
  exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "GOOGLE_SA_JSON_B64 (copie dans Railway) :"
echo "─────────────────────────────────────────────────────"
cat "$1" | base64 | tr -d '\n'
echo ""
echo "═══════════════════════════════════════════════════════"
echo ""
echo "✅ Valeur copiée dans le presse-papier (si pbcopy disponible)"
cat "$1" | base64 | tr -d '\n' | pbcopy 2>/dev/null && echo "   pbcopy OK" || echo "   (pbcopy non disponible — copie manuellement)"
echo ""
