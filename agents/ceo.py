"""
agents/ceo.py — CEO Welldone Studio : lit la queue Paperclip et dispatche aux agents.

Déclenché par :
  - Paperclip wakeOnDemand (tâche assignée)
  - Cron Railway 8h00 EST quotidien → POST /paperclip/ceo

Logique :
  1. Lire issues status='todo' dans Paperclip DB
  2. Mapper chaque issue → agent + commande selon le titre/description
  3. Dispatcher (asyncio) — un agent par issue
  4. Mettre à jour le status Paperclip : in_progress → done / failed
  5. Notifier JP via Telegram en fin de batch
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from agents._base import BaseAgent

log = logging.getLogger(__name__)

_DB_URL = os.environ.get(
    "PAPERCLIP_DB_URL",
    "postgresql://paperclip:paperclip@localhost:54329/paperclip"
)

# ── Mapping mots-clés → (agent, commande) ─────────────────────────────────────
_ROUTING_RULES: list[tuple[list[str], str, str]] = [
    # mots-clés dans le titre/description → agent, commande
    (["blog", "article", "rédiger", "rédaction", "contenu"],        "blog",      "rédiger"),
    (["seo", "analytics", "ga4", "trafic", "rapport analytics"],    "analytics", "rapport"),
    (["framer", "cms", "page web", "site"],                         "framer",    "liste"),
    (["email", "infolettre", "newsletter", "courriel"],             "email",     "trier"),
    (["veille", "tendance", "marché"],                              "veille",    "run"),
    (["audit", "analyse", "stratégique"],                           "analytics",       "rapport"),
    (["layout", "guardian", "mise en page", "responsive", "overflow", "alignment", "design qa"], "layout_guardian", "inspecter"),
]

def _route_issue(title: str, description: str) -> tuple[str, str] | None:
    """Retourne (agent, commande) selon le contenu de l'issue, ou None si non mappable."""
    text = (title + " " + (description or "")).lower()
    for keywords, agent, cmd in _ROUTING_RULES:
        if any(kw in text for kw in keywords):
            return agent, cmd
    return None


async def _get_db_conn():
    """Retourne une connexion psycopg2 à Paperclip."""
    import psycopg2
    return psycopg2.connect(_DB_URL)


async def _fetch_pending_issues() -> list[dict]:
    """Lit les issues status='todo' dans Paperclip."""
    try:
        conn = await asyncio.get_event_loop().run_in_executor(None, lambda: __import__('psycopg2').connect(_DB_URL))
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, description, assignee_agent_id
            FROM issues
            WHERE status = 'todo'
            ORDER BY created_at ASC
            LIMIT 10
        """)
        rows = cur.fetchall()
        conn.close()
        return [{"id": str(r[0]), "title": r[1] or "", "description": r[2] or "", "agent_id": r[3]} for r in rows]
    except Exception as e:
        log.error(f"ceo: erreur lecture Paperclip DB: {e}")
        return []


async def _update_issue_status(issue_id: str, status: str, note: str = "") -> bool:
    """Met à jour le status d'une issue dans Paperclip."""
    import datetime
    try:
        conn = await asyncio.get_event_loop().run_in_executor(None, lambda: __import__('psycopg2').connect(_DB_URL))
        cur = conn.cursor()
        now = datetime.datetime.utcnow()
        if status == "in_progress":
            cur.execute("UPDATE issues SET status=%s, started_at=%s, updated_at=%s WHERE id=%s",
                       (status, now, now, issue_id))
        elif status in ("done", "cancelled"):
            cur.execute("UPDATE issues SET status=%s, completed_at=%s, updated_at=%s WHERE id=%s",
                       (status, now, now, issue_id))
        else:
            cur.execute("UPDATE issues SET status=%s, updated_at=%s WHERE id=%s",
                       (status, now, issue_id))
        conn.commit()
        conn.close()
        log.info(f"ceo: issue {issue_id} → {status}")
        return True
    except Exception as e:
        log.error(f"ceo: erreur update issue {issue_id}: {e}")
        return False


class CEOAgent(BaseAgent):
    name        = "ceo"
    description = "CEO Welldone Studio — lit la queue Paperclip et dispatche aux agents"
    schedules: list = []

    @property
    def commands(self):
        return {"dispatch": self.dispatch, "status": self.status}

    async def dispatch(self, context: dict | None = None) -> str:
        """
        Lit les issues Paperclip status='todo', dispatche aux agents, met à jour le statut.
        Appelé par cron 8h ou par Paperclip wakeOnDemand.
        """
        from core.telegram_notifier import notify
        from core.dispatcher import dispatch as agent_dispatch

        ctx = context or {}
        log.info("ceo.dispatch: démarrage — lecture queue Paperclip")

        issues = await _fetch_pending_issues()
        if not issues:
            log.info("ceo.dispatch: aucune issue 'todo' en attente")
            return "✅ Aucune tâche en attente dans Paperclip."

        log.info(f"ceo.dispatch: {len(issues)} issue(s) trouvée(s)")
        await notify(
            f"🤖 *CEO — {len(issues)} tâche(s) en cours*\n"
            + "\n".join(f"• {i['title'][:60]}" for i in issues)
        )

        results = []
        for issue in issues:
            title       = issue["title"]
            description = issue["description"]
            issue_id    = issue["id"]

            # Router vers le bon agent
            route = _route_issue(title, description)
            if not route:
                log.warning(f"ceo.dispatch: issue non mappable — '{title[:60]}'")
                await _update_issue_status(issue_id, "cancelled")
                results.append(f"⚠️ Non mappable : {title[:50]}")
                continue

            agent_name, command = route
            log.info(f"ceo.dispatch: '{title[:50]}' → {agent_name}.{command}")

            # Marquer in_progress dans Paperclip
            await _update_issue_status(issue_id, "in_progress")

            # Dispatcher à l'agent
            try:
                # Extraire le sujet depuis le titre (retire les prefixes courants)
                sujet = re.sub(
                    r'^(mission|tâche|task|to.?do|rédiger|blog|article)\s*[:\-–]?\s*',
                    '', title, flags=re.IGNORECASE
                ).strip() or title

                result = await asyncio.wait_for(
                    agent_dispatch(agent_name, command, {"sujet": sujet, "issue_id": issue_id}),
                    timeout=360,  # 6 min max par tâche
                )
                # Vérifier si le résultat indique un echec réel
                result_str = str(result or "")
                if result_str.startswith("❌") or result_str.startswith("Erreur"):
                    await _update_issue_status(issue_id, "cancelled")
                    results.append(f"❌ {title[:50]} — {result_str[:80]}")
                    log.warning(f"ceo.dispatch: issue {issue_id} échouée — {result_str[:80]}")
                else:
                    await _update_issue_status(issue_id, "done")
                    results.append(f"✅ {title[:50]}")
                    log.info(f"ceo.dispatch: issue {issue_id} terminée")

            except asyncio.TimeoutError:
                log.error(f"ceo.dispatch: timeout issue {issue_id}")
                await _update_issue_status(issue_id, "cancelled")
                results.append(f"⏱ Timeout : {title[:50]}")

            except Exception as e:
                log.error(f"ceo.dispatch: erreur issue {issue_id}: {e}", exc_info=True)
                await _update_issue_status(issue_id, "cancelled")
                results.append(f"❌ Erreur : {title[:50]} — {str(e)[:80]}")

        # Rapport final à JP
        summary = "\n".join(results)
        await notify(
            f"📋 *CEO — Rapport d'exécution*\n\n{summary}\n\n"
            f"_{len([r for r in results if r.startswith('✅')])} réussie(s) / "
            f"{len(results)} totale(s)_"
        )
        return summary

    async def status(self, context: dict | None = None) -> str:
        """Affiche un résumé de la queue Paperclip."""
        issues = await _fetch_pending_issues()
        if not issues:
            return "📭 Aucune tâche en attente dans Paperclip."
        lines = [f"📋 *{len(issues)} tâche(s) en attente :*"]
        for i in issues:
            route = _route_issue(i["title"], i["description"])
            agent_info = f"→ {route[0]}.{route[1]}" if route else "→ ⚠️ non mappable"
            lines.append(f"• {i['title'][:60]} {agent_info}")
        return "\n".join(lines)


agent = CEOAgent()
