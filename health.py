#!/usr/bin/env python3
"""
health.py — Vérifie toutes les connexions avant deploy.

Usage :
  python health.py            → teste tout
  python health.py gmail      → teste seulement Gmail
  python health.py --telegram → envoie le rapport sur Telegram

Chaque check retourne {"service": ..., "status": "ok"|"error", "detail": ...}
Code de sortie 0 si tout est OK, 1 si au moins un service est en erreur.
"""
import sys, os, json, asyncio
from datetime import datetime

# Charger les variables d'env depuis .env si présent (dev local)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def check_google_oauth() -> dict:
    try:
        from core.auth import get_oauth_creds
        from googleapiclient.discovery import build
        creds = get_oauth_creds()
        svc = build("gmail", "v1", credentials=creds)
        profile = svc.users().getProfile(userId="me").execute()
        return {"service": "Gmail (OAuth)", "status": "ok", "detail": profile.get("emailAddress")}
    except Exception as e:
        return {"service": "Gmail (OAuth)", "status": "error", "detail": str(e)}


def check_google_service_account() -> dict:
    try:
        from core.auth import get_service_account_creds
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric
        from config import GA4_PROPERTY_ID

        scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
        creds  = get_service_account_creds(scopes)
        client = BetaAnalyticsDataClient(credentials=creds)
        req = RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            metrics=[Metric(name="sessions")],
            date_ranges=[DateRange(start_date="yesterday", end_date="today")],
            limit=1,
        )
        client.run_report(req)
        return {"service": "GA4 (Service Account)", "status": "ok", "detail": f"Property {GA4_PROPERTY_ID}"}
    except Exception as e:
        return {"service": "GA4 (Service Account)", "status": "error", "detail": str(e)}


def check_google_calendar() -> dict:
    try:
        from core.auth import get_google_service
        svc = get_google_service("calendar", "v3")
        cal = svc.calendarList().list(maxResults=1).execute()
        name = cal["items"][0]["summary"] if cal.get("items") else "calendrier trouvé"
        return {"service": "Google Calendar", "status": "ok", "detail": name}
    except Exception as e:
        return {"service": "Google Calendar", "status": "error", "detail": str(e)}


def check_notion() -> dict:
    try:
        import urllib.request
        from core.auth import get_notion_headers
        req = urllib.request.Request(
            "https://api.notion.com/v1/users/me",
            headers=get_notion_headers(),
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return {"service": "Notion", "status": "ok", "detail": data.get("name", "ok")}
    except Exception as e:
        return {"service": "Notion", "status": "error", "detail": str(e)}


def check_zoho() -> dict:
    try:
        import requests
        from core.auth import get_zoho_access_token
        from config import ZOHO_BASE_URL, ZOHO_ORG_ID
        token = get_zoho_access_token()
        resp  = requests.get(
            f"{ZOHO_BASE_URL}/invoices",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"organization_id": ZOHO_ORG_ID, "per_page": 1},
            timeout=5,
        )
        resp.raise_for_status()
        return {"service": "Zoho Books", "status": "ok", "detail": "API accessible"}
    except Exception as e:
        return {"service": "Zoho Books", "status": "error", "detail": str(e)}


def check_anthropic() -> dict:
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {"service": "Anthropic Claude", "status": "ok", "detail": f"model={CLAUDE_MODEL}"}
    except Exception as e:
        return {"service": "Anthropic Claude", "status": "error", "detail": str(e)}


def check_telegram() -> dict:
    try:
        import requests
        from config import TELEGRAM_BOT_TOKEN
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",
            timeout=5,
        )
        resp.raise_for_status()
        bot = resp.json()["result"]
        return {"service": "Telegram Bot", "status": "ok", "detail": f"@{bot['username']}"}
    except Exception as e:
        return {"service": "Telegram Bot", "status": "error", "detail": str(e)}


# ── Registre des checks ────────────────────────────────────────────────────────
CHECKS = {
    "gmail":     check_google_oauth,
    "ga4":       check_google_service_account,
    "calendar":  check_google_calendar,
    "notion":    check_notion,
    "zoho":      check_zoho,
    "anthropic": check_anthropic,
    "telegram":  check_telegram,
}


def run_checks(targets: list[str] | None = None) -> list[dict]:
    checks = {k: v for k, v in CHECKS.items() if not targets or k in targets}
    results = []
    for name, fn in checks.items():
        print(f"  Checking {name}...", end=" ", flush=True)
        result = fn()
        icon = "✅" if result["status"] == "ok" else "❌"
        print(f"{icon} {result['detail']}")
        results.append(result)
    return results


def format_report(results: list[dict]) -> str:
    lines = [f"🏥 Health Check — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"]
    for r in results:
        icon = "✅" if r["status"] == "ok" else "❌"
        lines.append(f"{icon} {r['service']}: {r['detail']}")
    errors = [r for r in results if r["status"] == "error"]
    if errors:
        lines.append(f"\n⚠️ {len(errors)} service(s) en erreur")
    else:
        lines.append("\n✅ Tous les services sont opérationnels")
    return "\n".join(lines)


async def send_telegram_report(report: str):
    from telegram import Bot
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_ALLOWED_USER_ID, text=report)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    targets = args if args else None

    print(f"\n{'═'*50}")
    print("  WELLDONE — Health Check")
    print(f"{'═'*50}\n")

    results = run_checks(targets)
    report  = format_report(results)

    print(f"\n{report}\n")

    if "--telegram" in flags:
        asyncio.run(send_telegram_report(report))
        print("📱 Rapport envoyé sur Telegram")

    errors = [r for r in results if r["status"] == "error"]
    sys.exit(1 if errors else 0)
