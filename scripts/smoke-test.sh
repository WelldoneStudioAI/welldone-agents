#!/usr/bin/env bash
# scripts/smoke-test.sh — Smoke test métier pour welldone-agents
#
# Usage:
#   ./scripts/smoke-test.sh                    → test prod (Railway URL)
#   ./scripts/smoke-test.sh http://localhost:8080 → test local
#
# Retourne 0 si FUNCTIONAL, 1 si DEPLOYED_NOT_VALIDATED ou FAILED
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE_URL="${1:-${RAILWAY_PUBLIC_DOMAIN:+https://$RAILWAY_PUBLIC_DOMAIN}}"
BASE_URL="${BASE_URL:-http://localhost:8080}"

PASS=0
FAIL=0
VERDICT="FUNCTIONAL"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  WELLDONE AGENTS — Smoke Test                ║"
echo "╚══════════════════════════════════════════════╝"
echo "  URL: $BASE_URL"
echo ""

# ── Fonction de test ──────────────────────────────────────────────────────────
check() {
    local name="$1"
    local url="$2"
    local expect_key="${3:-ok}"
    local expect_val="${4:-true}"

    echo -n "  [$name] ... "
    local response
    response=$(curl -sf --max-time 10 "$url" 2>/dev/null || echo '{"_curl_error":true}')

    if echo "$response" | grep -q "\"$expect_key\""; then
        local actual
        actual=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('$expect_key','')).lower())" 2>/dev/null || echo "")
        if [[ "$actual" == "$expect_val" ]]; then
            echo "✅ PASS"
            PASS=$((PASS + 1))
        else
            echo "❌ FAIL — '$expect_key'='$actual' (attendu: '$expect_val')"
            echo "     Réponse: $(echo "$response" | head -c 200)"
            FAIL=$((FAIL + 1))
            VERDICT="DEPLOYED_NOT_VALIDATED"
        fi
    else
        echo "❌ FAIL — clé '$expect_key' absente"
        echo "     Réponse: $(echo "$response" | head -c 200)"
        FAIL=$((FAIL + 1))
        VERDICT="DEPLOYED_NOT_VALIDATED"
    fi
}

# ── Test 1 : /livez — process vivant ─────────────────────────────────────────
check "livez — process vivant" "$BASE_URL/livez" "ok" "true"

# ── Test 2 : /healthz — santé réelle ─────────────────────────────────────────
echo -n "  [healthz — santé complète] ... "
HEALTHZ=$(curl -sf --max-time 15 "$BASE_URL/healthz" 2>/dev/null || echo '{"_curl_error":true}')

if echo "$HEALTHZ" | grep -q '"ok"'; then
    OK=$(echo "$HEALTHZ" | python3 -c "import sys,json; print(str(json.load(sys.stdin).get('ok','')).lower())" 2>/dev/null || echo "")
    COMMIT=$(echo "$HEALTHZ" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit','?'))" 2>/dev/null || echo "?")
    LOADED=$(echo "$HEALTHZ" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('agents_loaded',[])))" 2>/dev/null || echo "")
    FAILED=$(echo "$HEALTHZ" | python3 -c "import sys,json; d=json.load(sys.stdin); f=d.get('agents_failed',{}); print(','.join(f.keys()) if f else 'none')" 2>/dev/null || echo "?")
    MISSING=$(echo "$HEALTHZ" | python3 -c "import sys,json; d=json.load(sys.stdin); m=d.get('missing_vars',[]); print(','.join(m) if m else 'none')" 2>/dev/null || echo "?")

    echo "✅ PASS"
    echo "     commit         : $COMMIT"
    echo "     agents chargés : $LOADED"
    echo "     agents échoués : $FAILED"
    echo "     vars manquantes: $MISSING"
    PASS=$((PASS + 1))

    # Sous-vérifications
    if [[ "$FAILED" != "none" && "$FAILED" != "" ]]; then
        echo "  [agents_failed] ❌ FAIL — agents en échec: $FAILED"
        FAIL=$((FAIL + 1))
        VERDICT="DEPLOYED_NOT_VALIDATED"
    else
        echo "  [agents_failed] ✅ PASS — aucun agent en échec"
        PASS=$((PASS + 1))
    fi

    if [[ "$MISSING" != "none" && "$MISSING" != "" ]]; then
        echo "  [missing_vars]  ❌ FAIL — variables manquantes: $MISSING"
        FAIL=$((FAIL + 1))
        VERDICT="DEPLOYED_NOT_VALIDATED"
    else
        echo "  [missing_vars]  ✅ PASS — toutes les vars critiques présentes"
        PASS=$((PASS + 1))
    fi

    # Vérifier que reviseur est bien chargé
    if echo "$LOADED" | grep -q "reviseur"; then
        echo "  [agent:reviseur]✅ PASS — reviseur chargé"
        PASS=$((PASS + 1))
    else
        echo "  [agent:reviseur]❌ FAIL — reviseur ABSENT des agents chargés"
        FAIL=$((FAIL + 1))
        VERDICT="DEPLOYED_NOT_VALIDATED"
    fi

    # Vérifier que framer est bien chargé
    if echo "$LOADED" | grep -q "framer"; then
        echo "  [agent:framer]  ✅ PASS — framer chargé"
        PASS=$((PASS + 1))
    else
        echo "  [agent:framer]  ❌ FAIL — framer ABSENT"
        FAIL=$((FAIL + 1))
        VERDICT="DEPLOYED_NOT_VALIDATED"
    fi

else
    echo "❌ FAIL — /healthz inaccessible"
    echo "     Réponse: $(echo "$HEALTHZ" | head -c 200)"
    FAIL=$((FAIL + 1))
    VERDICT="FAILED"
fi

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────"
echo "  Tests passés : $PASS"
echo "  Tests échoués: $FAIL"
echo ""

if [[ "$VERDICT" == "FUNCTIONAL" ]]; then
    echo "  [VALIDATION] final_status=FUNCTIONAL ✅"
    echo "  [VALIDATION] smoke_test=PASS"
    EXIT_CODE=0
elif [[ "$VERDICT" == "DEPLOYED_NOT_VALIDATED" ]]; then
    echo "  [VALIDATION] final_status=DEPLOYED_NOT_VALIDATED ⚠️"
    echo "  [VALIDATION] smoke_test=FAIL"
    EXIT_CODE=1
else
    echo "  [VALIDATION] final_status=FAILED ❌"
    echo "  [VALIDATION] smoke_test=FAIL"
    EXIT_CODE=1
fi

echo "──────────────────────────────────────────────"
echo ""

exit $EXIT_CODE
