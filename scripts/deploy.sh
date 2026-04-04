#!/usr/bin/env bash
# scripts/deploy.sh — Déploiement Railway GARANTI avec vérification du commit.
#
# Usage: ./scripts/deploy.sh
#
# Ce script :
# 1. Lit le HEAD SHA local
# 2. Vérifie que le commit est pushé sur GitHub
# 3. Déclenche le deploy Railway avec commitSha EXPLICITE
# 4. Attend le build (poll toutes les 10s, max 5 min)
# 5. Vérifie que le commit déployé == HEAD local
# 6. Affiche SUCCESS ou FAIL clair
#
# Nécessite : ~/.railway/config.json avec user.token

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SERVICE_ID="f5c50e4a-3ab9-4dae-b4fd-c8c354ad63d8"
ENV_ID="5d7a6514-00c3-4b3d-a2f7-7f2b64981943"
RAILWAY_API="https://backboard.railway.com/graphql/v2"
MAX_WAIT=300  # 5 minutes max
POLL_INTERVAL=10

# ── Couleurs ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Railway token ─────────────────────────────────────────────────────────────
RAILWAY_CONFIG="$HOME/.railway/config.json"
if [[ ! -f "$RAILWAY_CONFIG" ]]; then
  echo -e "${RED}ERREUR: $RAILWAY_CONFIG introuvable${NC}"
  exit 1
fi
TOKEN=$(python3 -c "import json; print(json.load(open('$RAILWAY_CONFIG'))['user']['token'])")

# ── 1. Commit local ──────────────────────────────────────────────────────────
LOCAL_SHA=$(git rev-parse HEAD)
LOCAL_MSG=$(git log -1 --pretty=format:"%s" HEAD)
echo -e "${CYAN}━━━ Deploy Railway ━━━${NC}"
echo -e "Commit local : ${GREEN}${LOCAL_SHA:0:12}${NC}"
echo -e "Message      : ${LOCAL_MSG}"
echo ""

# ── 2. Vérifier que le commit est pushé ──────────────────────────────────────
echo -n "Vérification push GitHub... "
REMOTE_SHA=$(git ls-remote origin HEAD 2>/dev/null | cut -f1)
if [[ "$REMOTE_SHA" != "$LOCAL_SHA" ]]; then
  echo -e "${RED}MISMATCH${NC}"
  echo -e "${RED}Local  : $LOCAL_SHA${NC}"
  echo -e "${RED}Remote : $REMOTE_SHA${NC}"
  echo -e "${YELLOW}→ Fais 'git push origin main' d'abord.${NC}"
  exit 1
fi
echo -e "${GREEN}OK${NC}"

# ── 3. Déclencher le deploy avec commitSha EXPLICITE ─────────────────────────
echo -n "Déclenchement deploy... "
DEPLOY_RESULT=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"query\":\"mutation { serviceInstanceDeployV2(serviceId: \\\"$SERVICE_ID\\\", environmentId: \\\"$ENV_ID\\\", commitSha: \\\"$LOCAL_SHA\\\") }\"}" \
  "$RAILWAY_API")

DEPLOY_ID=$(echo "$DEPLOY_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('serviceInstanceDeployV2',''))" 2>/dev/null || echo "")

if [[ -z "$DEPLOY_ID" ]]; then
  echo -e "${RED}ÉCHEC${NC}"
  echo "$DEPLOY_RESULT" | python3 -m json.tool 2>/dev/null || echo "$DEPLOY_RESULT"
  exit 1
fi
echo -e "${GREEN}$DEPLOY_ID${NC}"

# ── 4. Attendre le build ─────────────────────────────────────────────────────
echo ""
ELAPSED=0
while [[ $ELAPSED -lt $MAX_WAIT ]]; do
  STATUS_RESULT=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"query\":\"query { deployments(first:1, input:{serviceId:\\\"$SERVICE_ID\\\", environmentId:\\\"$ENV_ID\\\"}) { edges { node { id status meta } } } }\"}" \
    "$RAILWAY_API")

  STATUS=$(echo "$STATUS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['deployments']['edges'][0]['node']['status'])" 2>/dev/null || echo "UNKNOWN")
  DEPLOYED_SHA=$(echo "$STATUS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['deployments']['edges'][0]['node']['meta']['commitHash'])" 2>/dev/null || echo "")
  DEPLOYED_ID=$(echo "$STATUS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['deployments']['edges'][0]['node']['id'])" 2>/dev/null || echo "")

  echo -ne "\r⏳ ${ELAPSED}s — ${STATUS} (deploy: ${DEPLOYED_ID:0:12}, commit: ${DEPLOYED_SHA:0:12})   "

  if [[ "$STATUS" == "SUCCESS" && "$DEPLOYED_SHA" == "$LOCAL_SHA" ]]; then
    echo ""
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  ✅ DEPLOY CONFIRMÉ                                  ${NC}"
    echo -e "${GREEN}  Commit : ${LOCAL_SHA:0:12} ($LOCAL_MSG)${NC}"
    echo -e "${GREEN}  Deploy : $DEPLOY_ID${NC}"
    echo -e "${GREEN}  Durée  : ${ELAPSED}s${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 0
  fi

  if [[ "$STATUS" == "FAILED" || "$STATUS" == "CRASHED" ]]; then
    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}  ❌ DEPLOY ÉCHOUÉ — status: $STATUS                   ${NC}"
    echo -e "${RED}  Deploy : $DEPLOYED_ID${NC}"
    echo -e "${RED}  → Vérifie les logs Railway                          ${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
  fi

  if [[ "$STATUS" == "SUCCESS" && "$DEPLOYED_SHA" != "$LOCAL_SHA" ]]; then
    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}  ⚠️  MAUVAIS COMMIT DÉPLOYÉ                          ${NC}"
    echo -e "${RED}  Attendu : ${LOCAL_SHA:0:12}${NC}"
    echo -e "${RED}  Déployé : ${DEPLOYED_SHA:0:12}${NC}"
    echo -e "${RED}  → Le commitSha n'a pas été pris en compte           ${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
  fi

  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

echo ""
echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${RED}  ⏱️  TIMEOUT (${MAX_WAIT}s) — dernier status: $STATUS     ${NC}"
echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
exit 1
