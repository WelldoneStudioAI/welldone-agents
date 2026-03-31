"""
agents/notion.py — Agent Notion.
Capacités: créer une tâche, chercher dans les pages, stocker les outputs IA.
"""
import json, logging, os, urllib.request
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

    async def store_output(self, context: dict | None = None) -> str | None:
        """
        Stocke un output IA dans une base Notion "Outputs IA".
        context: {
            "titre": str,
            "contenu": str,
            "type": str,           # commande (ex: "rédiger", "rapport")
            "source_agent": str,   # agent qui a produit le résultat
            "lien": str (optionnel)
        }
        Retourne l'URL de la page créée ou None si échec.
        0 appels Claude — uniquement API Notion directe. Timeout: 10s.
        """
        ctx          = context or {}
        titre        = ctx.get("titre", "Output IA")
        contenu      = ctx.get("contenu", "")
        type_cmd     = ctx.get("type", "")
        source_agent = ctx.get("source_agent", "")
        lien         = ctx.get("lien", "")

        # Base Outputs IA — utiliser une variable env dédiée ou fallback sur NOTION_TASK_DB
        outputs_db = os.environ.get("NOTION_OUTPUTS_DB", NOTION_TASK_DB)

        props: dict = {
            "Nom": {"title": [{"text": {"content": titre[:200]}}]},
            "Créé par IA": {"checkbox": True},
        }

        # Propriétés optionnelles — ne pas crasher si elles n'existent pas dans la DB
        if type_cmd:
            props["Type"] = {"select": {"name": type_cmd[:50]}}
        if source_agent:
            props["Agent"] = {"select": {"name": source_agent[:50]}}
        if lien:
            props["Lien"] = {"url": lien}

        # Contenu comme bloc paragraphe (max 2000 chars par bloc Notion)
        children = []
        if contenu:
            chunks = [contenu[i:i+2000] for i in range(0, min(len(contenu), 10000), 2000)]
            for chunk in chunks:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                })

        payload: dict = {
            "parent": {"database_id": outputs_db},
            "properties": props,
        }
        if children:
            payload["children"] = children

        try:
            import urllib.request as _req
            body = json.dumps(payload).encode()
            request = _req.Request(
                "https://api.notion.com/v1/pages",
                data=body,
                headers=get_notion_headers(),
                method="POST",
            )
            resp = _req.urlopen(request, timeout=10)
            page = json.loads(resp.read())
            url = page.get("url", "")
            log.info(f"notion.store_output: page créée titre={titre} url={url}")
            return url or None
        except Exception as e:
            log.error(f"notion.store_output error: {e}")
            return None

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
