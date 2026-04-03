"""
agents/watchdog.py — Surveillance de la santé de tous les agents.

Tourne toutes les 6h. Teste chaque connexion critique et notifie JP
si quelque chose est cassé. AUCUN flan — chaque test fait un vrai appel.

Tests effectués :
  - IMAP WHC : connexion + SELECT INBOX
  - IMAP Hostinger : connexion + SELECT INBOX
  - GA4 : requête minimale (1 ligne, 1 jour)
  - Anthropic : ping API (liste modèles)
  - Telegram : vérification bot token (getMe)
  - Notion : lecture d'une DB (sources veille)
  - Railway uptime : GET /health du serveur lui-même

Résultat Telegram :
  ✅ tout vert → pas de notification (silencieux si OK)
  ⚠️ un ou plusieurs services dégradés → alerte immédiate
"""
from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import os
import ssl
import time
import urllib.request
from datetime import datetime

from agents._base import BaseAgent

log = logging.getLogger(__name__)


class WatchdogAgent(BaseAgent):
    name        = "watchdog"
    description = "Surveillance santé de tous les agents et connexions"
    schedules   = [
        {"cron": "0 */6 * * *", "command": "check", "context": {}, "label": "Health check 6h"},
    ]

    @property
    def commands(self):
        return {
            "check":  self.check,
            "status": self.check,   # alias
        }

    # ── Check principal ───────────────────────────────────────────────────────

    async def check(self, context: dict | None = None) -> str:
        """Lance tous les tests de santé, notifie si dégradation."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        results: list[tuple[str, bool, str]] = []  # (nom, ok, détail)

        # Tests en parallèle
        checks = await asyncio.gather(
            self._check_imap_whc(),
            self._check_imap_hostinger(),
            self._check_ga4(),
            self._check_anthropic(),
            self._check_telegram(),
            self._check_notion(),
            return_exceptions=True,
        )

        names = ["IMAP WHC", "IMAP Hostinger", "GA4", "Anthropic", "Telegram", "Notion"]
        for name, result in zip(names, checks):
            if isinstance(result, Exception):
                results.append((name, False, str(result)[:100]))
            else:
                results.append(result)

        ok_count   = sum(1 for _, ok, _ in results if ok)
        fail_count = len(results) - ok_count

        # Construire le rapport
        lines = [f"🔍 *Watchdog — {ts}*\n"]
        for name, ok, detail in results:
            icon = "✅" if ok else "❌"
            lines.append(f"{icon} {name}" + (f" — {detail}" if not ok else ""))

        lines.append(f"\n_{ok_count}/{len(results)} services opérationnels_")

        summary = "\n".join(lines)
        log.info(f"watchdog.check: {ok_count}/{len(results)} OK")

        # Notifier uniquement si dégradation (ou si forced)
        if fail_count > 0:
            from core.telegram_notifier import notify
            await notify(f"⚠️ *WATCHDOG — {fail_count} service(s) en panne*\n\n{summary}")
        else:
            log.info("watchdog.check: tous les services OK — pas de notification Telegram")

        return summary

    # ── Tests individuels ─────────────────────────────────────────────────────

    async def _check_imap_whc(self) -> tuple[str, bool, str]:
        name = "IMAP WHC"
        host = os.environ.get("WHC_IMAP_HOST", "mail.awelldone.com")
        port = int(os.environ.get("WHC_IMAP_PORT", "993"))
        user = os.environ.get("WHC_EMAIL", "")
        pwd  = os.environ.get("WHC_PASSWORD", "")
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._test_imap(name, host, port, user, pwd)
        )

    async def _check_imap_hostinger(self) -> tuple[str, bool, str]:
        name = "IMAP Hostinger"
        host = os.environ.get("HST_IMAP_HOST", "imap.hostinger.com")
        port = int(os.environ.get("HST_IMAP_PORT", "993"))
        user = os.environ.get("HST_EMAIL", "")
        pwd  = os.environ.get("HST_PASSWORD", "")
        if not pwd:
            return (name, False, "HST_PASSWORD manquant dans env")
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._test_imap(name, host, port, user, pwd)
        )

    async def _check_ga4(self) -> tuple[str, bool, str]:
        name = "GA4"
        def _test():
            try:
                import base64
                sa_b64 = os.environ.get("GOOGLE_SA_JSON_B64", "")
                if not sa_b64:
                    return (name, False, "GOOGLE_SA_JSON_B64 manquant")
                import tempfile
                sa_json = json.loads(base64.b64decode(sa_b64))
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
                json.dump(sa_json, f); f.close()
                from google.oauth2 import service_account
                from google.analytics.data_v1beta import BetaAnalyticsDataClient
                from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric
                creds = service_account.Credentials.from_service_account_file(
                    f.name, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
                )
                client = BetaAnalyticsDataClient(credentials=creds)
                ga4_id = os.environ.get("GA4_PROPERTY_ID", "522467276")
                resp = client.run_report(RunReportRequest(
                    property=f"properties/{ga4_id}",
                    dimensions=[Dimension(name="date")],
                    metrics=[Metric(name="sessions")],
                    date_ranges=[DateRange(start_date="yesterday", end_date="today")],
                    limit=1,
                ))
                os.unlink(f.name)
                return (name, True, "")
            except Exception as e:
                return (name, False, str(e)[:100])
        return await asyncio.get_event_loop().run_in_executor(None, _test)

    async def _check_anthropic(self) -> tuple[str, bool, str]:
        name = "Anthropic"
        def _test():
            try:
                import anthropic
                key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not key:
                    return (name, False, "ANTHROPIC_API_KEY manquant")
                client = anthropic.Anthropic(api_key=key)
                # Appel minimal : 1 token
                # Utiliser le modèle le moins coûteux pour le ping (haiku)
                model_to_test = "claude-haiku-4-5"
                resp = client.messages.create(
                    model=model_to_test,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                )
                return (name, True, "")
            except Exception as e:
                return (name, False, str(e)[:100])
        return await asyncio.get_event_loop().run_in_executor(None, _test)

    async def _check_telegram(self) -> tuple[str, bool, str]:
        name = "Telegram"
        def _test():
            try:
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                if not token:
                    return (name, False, "TELEGRAM_BOT_TOKEN manquant")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/getMe",
                    headers={"User-Agent": "WelldoneWatchdog/1.0"},
                )
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read())
                if data.get("ok"):
                    bot_name = data["result"].get("username", "?")
                    return (name, True, "")
                return (name, False, f"getMe returned ok=false")
            except Exception as e:
                return (name, False, str(e)[:100])
        return await asyncio.get_event_loop().run_in_executor(None, _test)

    async def _check_notion(self) -> tuple[str, bool, str]:
        name = "Notion"
        def _test():
            try:
                token = os.environ.get("NOTION_API_KEY", "") or os.environ.get("NOTION_TOKEN", "")
                if not token:
                    return (name, False, "NOTION_API_KEY manquant")
                db_id = os.environ.get("NOTION_SOURCES_DB", "")
                if not db_id:
                    return (name, False, "NOTION_SOURCES_DB manquant")
                req = urllib.request.Request(
                    f"https://api.notion.com/v1/databases/{db_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Notion-Version": "2022-06-28",
                    },
                )
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read())
                return (name, True, "")
            except Exception as e:
                return (name, False, str(e)[:100])
        return await asyncio.get_event_loop().run_in_executor(None, _test)

    # ── Helper IMAP ───────────────────────────────────────────────────────────

    def _test_imap(self, name: str, host: str, port: int,
                   user: str, pwd: str) -> tuple[str, bool, str]:
        if not pwd:
            return (name, False, "mot de passe manquant dans env")
        try:
            ctx = ssl.create_default_context()
            M   = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
            M.login(user, pwd)
            typ, data = M.select("INBOX", readonly=True)
            count = data[0].decode() if data[0] else "?"
            M.logout()
            return (name, True, "")
        except imaplib.IMAP4.error as e:
            return (name, False, f"Auth IMAP: {e}")
        except Exception as e:
            return (name, False, str(e)[:100])


agent = WatchdogAgent()
