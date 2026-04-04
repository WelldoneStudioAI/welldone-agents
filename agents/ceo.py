"""
agents/ceo.py — CEO Welldone Studio : briefing matin quotidien.

Déclenché par :
  - Cron Railway lun-ven 8h03 EST → POST /run/ceo/dispatch
  - Telegram manuel : /ceo dispatch

Ce que ça fait :
  - Envoie un message Telegram de démarrage de journée à JP
  - Liste les tâches Notion en attente (status 'todo' ou 'À faire')
  - Rappelle les commandes disponibles
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from agents._base import BaseAgent

log = logging.getLogger(__name__)

_JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
_MOIS_FR  = ["janvier", "février", "mars", "avril", "mai", "juin",
              "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


class CEOAgent(BaseAgent):
    name        = "ceo"
    description = "Briefing matin quotidien — résumé des tâches Notion + rappel des agents disponibles"
    schedules: list = []

    @property
    def commands(self):
        return {"dispatch": self.dispatch}

    async def dispatch(self, context: dict | None = None) -> str:
        """
        Briefing matin : tâches Notion en attente + agents disponibles.
        Appelé par cron 8h03 ou manuellement.
        """
        from core.telegram_notifier import notify
        from core.dispatcher import REGISTRY, discover_agents

        now = datetime.now()
        jour  = _JOURS_FR[now.weekday()]
        date  = f"{now.day} {_MOIS_FR[now.month - 1]} {now.year}"

        # Récupérer les tâches Notion en attente
        notion_summary = ""
        try:
            if not REGISTRY:
                discover_agents()
            if "notion" in REGISTRY:
                result = await asyncio.wait_for(
                    REGISTRY["notion"].run_command("task", {"title": "__list_pending__"}),
                    timeout=20,
                )
                if result and not result.startswith("❌"):
                    notion_summary = f"\n\n📋 *Tâches Notion :*\n{result[:400]}"
        except Exception as e:
            log.warning(f"ceo: notion fetch skip ({e})")

        msg = (
            f"🌅 *Bon matin, JP !*\n"
            f"_{jour} {date}_"
            f"{notion_summary}\n\n"
            f"*Agents disponibles :*\n"
            f"• `/blog rédiger <sujet>` — article complet\n"
            f"• `/analytics rapport` — SEO + GA4\n"
            f"• `/veille run` — veille hebdo\n"
            f"• `/email trier` — boîte WHC\n"
            f"• `/framer liste` — articles Framer"
        )

        await notify(msg)
        log.info("ceo.dispatch: briefing matin envoyé")
        return "✅ Briefing matin envoyé."


agent = CEOAgent()
