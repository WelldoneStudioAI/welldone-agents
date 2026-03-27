"""
agents/notion.py — Agent Notion.
Capacités: créer une tâche, chercher dans les pages.
"""
import json, logging, urllib.request
from datetime import datetime
from agents._base import BaseAgent
from core.auth import get_notion_headers
from config import NOTION_TASK_DB

log = logging.getLogger(__name__)


def _notion_req(method: str, path: str, data: dict | None = None) -> dict:
    body = json.dumps(data or {}).encode()
    req  = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=body,
        headers=get_notion_headers(),
        method=method,
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


class NotionAgent(BaseAgent):
    name        = "notion"
    description = "Créer des tâches et pages dans Notion"

    @property
    def commands(self):
        return {
            "task":   self.create_task,
            "search": self.search,
        }

    async def create_task(self, context: dict | None = None) -> str:
        """
        context attendu:
          title (str), priority (str) "Haute"|"Moyenne"|"Basse" [défaut: Moyenne],
          date (str "YYYY-MM-DD") [optionnel], notes (str) [optionnel]
        """
        ctx      = context or {}
        title    = ctx.get("title", "")
        priority = ctx.get("priority", "Moyenne")
        date_str = ctx.get("date")
        notes    = ctx.get("notes", "")

        if not title:
            return "❌ Paramètre 'title' manquant"

        props = {
            "Nom":         {"title": [{"text": {"content": title}}]},
            "Statut":      {"select": {"name": "À faire"}},
            "Priorité":    {"select": {"name": priority}},
            "Créé par IA": {"checkbox": True},
        }
        if date_str:
            props["Date"] = {"date": {"start": date_str}}
        if notes:
            props["Notes"] = {"rich_text": [{"text": {"content": notes[:2000]}}]}

        try:
            page = _notion_req("POST", "pages", {
                "parent": {"database_id": NOTION_TASK_DB},
                "properties": props,
            })
            url = page.get("url", "")
            log.info(f"notion.task created title={title}")
            return f"✅ Tâche créée dans Notion\n📋 *{title}*\n🚦 Priorité: {priority}\n🔗 {url}"
        except Exception as e:
            log.error(f"notion.task error: {e}")
            return f"❌ Erreur création tâche Notion: {e}"

    async def search(self, context: dict | None = None) -> str:
        """
        context: {"query": "texte à chercher"}
        """
        query = (context or {}).get("query", "")
        if not query:
            return "❌ Paramètre 'query' manquant"

        try:
            results = _notion_req("POST", "search", {
                "query": query,
                "page_size": 5,
            })
            pages = results.get("results", [])
            if not pages:
                return f"🔍 Aucun résultat Notion pour « {query} »"

            lines = [f"🔍 *Résultats Notion pour « {query} » :*\n"]
            for p in pages:
                props = p.get("properties", {})
                # Essayer de trouver le titre
                title = ""
                for v in props.values():
                    if v.get("type") == "title":
                        texts = v.get("title", [])
                        if texts:
                            title = texts[0].get("plain_text", "")
                            break
                url  = p.get("url", "")
                ptype = p.get("object", "page")
                lines.append(f"• [{ptype}] *{title or 'Sans titre'}*\n  {url}")

            return "\n".join(lines)
        except Exception as e:
            log.error(f"notion.search error: {e}")
            return f"❌ Erreur recherche Notion: {e}"


agent = NotionAgent()
