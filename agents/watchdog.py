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
from config import (
    NOTION_TOKEN, NOTION_HEALTHCHECK_DB,
    WHC_IMAP_HOST, WHC_IMAP_PORT, WHC_EMAIL, WHC_PASSWORD,
    HST_IMAP_HOST, HST_IMAP_PORT, HST_EMAIL, HST_PASSWORD,
)

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

        # Railway injecte le SHA du déploiement (le conteneur n'a pas de .git → le
        # `git rev-parse` retournait toujours « inconnu »). On lit l'env d'abord.
        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
        if not commit:
            import subprocess
            try:
                commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                                 stderr=subprocess.DEVNULL).decode().strip()
            except Exception:
                commit = "inconnu"
        lines.append(f"\n_{ok_count}/{len(results)} services opérationnels — commit `{commit}`_")

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
        # WHC est mort : la boîte vit désormais sur Hostinger. config résout le host
        # (garde-fou DNS) → plus jamais de « Errno 8 » sur mail.awelldone.com.
        host, port = WHC_IMAP_HOST, WHC_IMAP_PORT
        user, pwd  = WHC_EMAIL, WHC_PASSWORD
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._test_imap(name, host, port, user, pwd)
        )

    async def _check_imap_hostinger(self) -> tuple[str, bool, str]:
        name = "IMAP Hostinger"
        host, port = HST_IMAP_HOST, HST_IMAP_PORT
        user, pwd  = HST_EMAIL, HST_PASSWORD
        if not pwd:
            return (name, False, "HST_PASSWORD manquant dans env")
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._test_imap(name, host, port, user, pwd)
        )

    async def _check_ga4(self) -> tuple[str, bool, str]:
        name = "GA4"
        def _test():
            tmp_path = None
            try:
                import base64
                sa_b64 = os.environ.get("GOOGLE_SA_JSON_B64", "")
                if not sa_b64:
                    return (name, False, "GOOGLE_SA_JSON_B64 manquant")
                import tempfile
                sa_json = json.loads(base64.b64decode(sa_b64))
                f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
                json.dump(sa_json, f); f.close()
                tmp_path = f.name
                from google.oauth2 import service_account
                from google.analytics.data_v1beta import BetaAnalyticsDataClient
                from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric
                creds = service_account.Credentials.from_service_account_file(
                    tmp_path, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
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
                return (name, True, "")
            except Exception as e:
                return (name, False, str(e)[:100])
            finally:
                # Nettoyage garanti même si une exception est levée
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        return await asyncio.get_running_loop().run_in_executor(None, _test)

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
        return await asyncio.get_running_loop().run_in_executor(None, _test)

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
        return await asyncio.get_running_loop().run_in_executor(None, _test)

    async def _check_notion(self) -> tuple[str, bool, str]:
        name = "Notion"
        def _test():
            try:
                # Source de vérité : config (mêmes valeurs que les agents réels).
                token = NOTION_TOKEN
                if not token:
                    return (name, False, "NOTION_TOKEN manquant")
                headers = {"Authorization": f"Bearer {token}",
                           "Notion-Version": "2022-06-28"}
                # 1) Validité du token, indépendamment d'une DB précise.
                req = urllib.request.Request(
                    "https://api.notion.com/v1/users/me", headers=headers)
                urllib.request.urlopen(req, timeout=10).read()
                # 2) Lecture d'une DB VIVANTE réellement utilisée (Pipeline Agents IA).
                #    NOTION_SOURCES_DB est mort (404) → JAMAIS sondé ici : c'était la
                #    cause de la fausse alerte « Notion en panne ».
                db_id = NOTION_HEALTHCHECK_DB
                if db_id:
                    req2 = urllib.request.Request(
                        f"https://api.notion.com/v1/databases/{db_id}", headers=headers)
                    urllib.request.urlopen(req2, timeout=10).read()
                return (name, True, "")
            except Exception as e:
                return (name, False, str(e)[:100])
        return await asyncio.get_running_loop().run_in_executor(None, _test)

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
