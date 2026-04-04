"""
agents/veille.py — Agent Veille de contenu (chaque lundi 8h).
Pipeline: Notion sources → RSS → Claude → Notion page → Email.
"""
import json, logging, base64, urllib.request
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from agents._base import BaseAgent
from core.auth import get_notion_headers, get_oauth_creds
from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL,
    NOTION_SOURCES_DB, GMAIL_RECIPIENT,
)

log = logging.getLogger(__name__)


class VeilleAgent(BaseAgent):
    name        = "veille"
    description = "Veille de contenu hebdomadaire: RSS → idées d'articles → Notion + Email"
    schedules   = [{"cron": "0 13 * * 1", "command": "run"}]  # Lundi 8h05 Montreal

    @property
    def commands(self):
        return {"run": self.run}

    async def run(self, context: dict | None = None) -> str:
        """Pipeline complet de veille."""
        TODAY = datetime.today().strftime("%d %B %Y")
        try:
            log.info("veille.run start")
            sources  = self._get_sources()
            log.info(f"veille: {len(sources)} sources actives")

            articles = self._fetch_articles(sources)
            log.info(f"veille: {len(articles)} articles récupérés")

            idees = self._generate_ideas(articles)

            notion_url = ""
            try:
                notion_url = self._create_notion_page(idees)
                log.info(f"veille: Notion page créée {notion_url}")
            except Exception as e:
                log.warning(f"veille: Notion page skipped ({e})")

            self._send_email(idees, notion_url)
            log.info("veille.run done")

            # ── Pipeline Notion ─────────────────────────────────────────────────
            pipeline_url = None
            try:
                from core.notion_delivery import pipeline_create as _pipeline_create
                pipeline_url = await _pipeline_create(
                    title=f"Veille contenu — {TODAY}",
                    agent="veille",
                    type_="veille",
                    content=idees,
                    status="Prêt révision",
                )
            except Exception as _ne:
                log.warning(f"veille: notion pipeline skip ({_ne})")

            # Compter les idées réellement générées (lignes commençant par un chiffre)
            import re as _re
            idees_count  = len(_re.findall(r'^\s*\d+[\.\)]', idees, flags=_re.MULTILINE))
            notion_line  = "📋 Page Notion créée\n" if notion_url else "⚠️ Notion skipped\n"
            pipeline_line = f"📋 [Pipeline Notion]({pipeline_url})\n" if pipeline_url else ""
            return (
                f"✅ Veille {TODAY} complète !\n"
                f"📡 {len(sources)} sources · {len(articles)} articles récupérés\n"
                f"💡 {idees_count} idées générées\n"
                f"{notion_line}"
                f"{pipeline_line}"
                f"📧 Email envoyé à {GMAIL_RECIPIENT}"
            )
        except Exception as e:
            log.error(f"veille.run error: {e}")
            return f"❌ Erreur veille: {e}"

    # ── 1. Sources Notion ─────────────────────────────────────────────────────

    def _get_sources(self) -> list[dict]:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{NOTION_SOURCES_DB}/query",
            data=json.dumps({"filter": {"property": "Actif", "checkbox": {"equals": True}}}).encode(),
            headers=get_notion_headers(),
            method="POST",
        )
        resp    = urllib.request.urlopen(req, timeout=10)
        results = json.loads(resp.read()).get("results", [])

        sources = []
        for page in results:
            props = page["properties"]
            nom   = props["Nom"]["title"][0]["plain_text"] if props["Nom"]["title"] else ""
            rss   = (props["RSS"]["url"] or "") if props.get("RSS") else ""
            cat   = props["Catégorie"]["select"]["name"] if props.get("Catégorie") and props["Catégorie"]["select"] else ""
            lang  = props["Langue"]["select"]["name"] if props.get("Langue") and props["Langue"]["select"] else ""
            if rss:
                sources.append({"nom": nom, "rss": rss, "categorie": cat, "langue": lang})
        return sources

    # ── 2. RSS ────────────────────────────────────────────────────────────────

    def _fetch_articles(self, sources: list[dict], max_per_source: int = 4) -> list[dict]:
        import feedparser
        articles = []
        for src in sources:
            try:
                feed = feedparser.parse(src["rss"])
                for entry in feed.entries[:max_per_source]:
                    published = entry.get("published", "")[:10] if entry.get("published") else ""
                    summary   = entry.get("summary", entry.get("description", ""))[:500]
                    articles.append({
                        "titre":    entry.get("title", ""),
                        "url":      entry.get("link", ""),
                        "resume":   summary,
                        "source":   src["nom"],
                        "categorie": src["categorie"],
                        "langue":   src["langue"],
                        "date":     published,
                    })
            except Exception as e:
                log.warning(f"veille RSS error {src['nom']}: {e}")
        return articles

    # ── 3. Claude ─────────────────────────────────────────────────────────────

    def _generate_ideas(self, articles: list[dict]) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        articles_txt = "\n\n".join([
            f"[{a['source']} — {a['categorie']} — {a['langue']}]\n{a['titre']}\n{a['resume'][:300]}\nURL: {a['url']}"
            for a in articles[:30]
        ])

        prompt = f"""Tu es le stratège de contenu de Welldone Studio, une agence créative montréalaise fondée par Jean-Philippe Roy.

Welldone Studio se spécialise en photographie commerciale, vidéo, branding, et stratégie numérique pour les PME québécoises.
Ton auditoire : entrepreneurs et décideurs du Québec, en français, avec un ton direct, visuel, concret et professionnel.

Voici les derniers articles des sources de référence (marketing, SEO, IA) :

{articles_txt}

---

Génère exactement 10 idées d'articles de blog pour awelldone.com/journal.

Pour chaque idée :
1. **Titre accrocheur** (adapté au marché québécois, en français)
2. **Angle Welldone** (comment on l'adapte à notre positionnement visuel/créatif)
3. **Source d'inspiration** (le ou les articles originaux)
4. **Mots-clés SEO** (2-3 mots-clés cibles)
5. **Format suggéré** (liste, guide, étude de cas, opinion)

IMPORTANT :
- Jamais de traduction directe — réinterprète avec une perspective québécoise et créative
- Liens avec la photographie commerciale, le branding visuel, la stratégie numérique des PME
- Évite le jargon trop technique — ton accessible et inspirant

Format : numérote de 1 à 10, structure claire."""

        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    # ── 4. Notion page ────────────────────────────────────────────────────────

    def _create_notion_page(self, idees: str) -> str:
        chunks   = [idees[i:i+1900] for i in range(0, len(idees), 1900)]
        children = [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Coche tes 5 favoris avant mardi 8h ↓"}}]},
        }]
        for chunk in chunks:
            children.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })

        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=json.dumps({
                "parent": {"type": "workspace", "workspace": True},
                "properties": {"title": {"title": [{"text": {"content": f"Veille du {TODAY} — Sélection"}}]}},
                "children": children,
            }).encode(),
            headers=get_notion_headers(),
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())["url"]

    # ── 5. Email ──────────────────────────────────────────────────────────────

    def _send_email(self, idees: str, notion_url: str):
        from googleapiclient.discovery import build

        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }}
  .container {{ max-width: 680px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; }}
  .header {{ background: #0a0a0a; color: white; padding: 32px 40px; }}
  .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; }}
  .header p {{ margin: 8px 0 0; color: #888; font-size: 14px; }}
  .body {{ padding: 40px; }}
  .cta {{ display: inline-block; background: #0a0a0a; color: white; padding: 14px 28px; border-radius: 6px; text-decoration: none; font-weight: 600; margin: 24px 0; }}
  .idees {{ white-space: pre-wrap; font-size: 14px; line-height: 1.7; color: #333; background: #f9f9f9; padding: 24px; border-radius: 6px; border-left: 3px solid #0a0a0a; }}
  .footer {{ padding: 24px 40px; background: #f5f5f5; color: #888; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Veille Welldone — {TODAY}</h1>
    <p>10 idées d'articles · Sélectionne tes 5 favoris avant mardi 8h</p>
  </div>
  <div class="body">
    <p>Bonjour JP,</p>
    <p>Voici ta sélection hebdomadaire générée depuis tes sources de référence.</p>
    {'<a href="' + notion_url + '" class="cta">Ouvrir dans Notion pour cocher tes 5 choix →</a>' if notion_url else ''}
    <div class="idees">{idees}</div>
  </div>
  <div class="footer">
    Welldone Studio · Veille automatisée chaque lundi 8h
  </div>
</div>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Veille Welldone — {TODAY} — 10 idées à approuver"
        msg["From"]    = GMAIL_RECIPIENT
        msg["To"]      = GMAIL_RECIPIENT
        msg.attach(MIMEText(html, "html"))

        creds   = get_oauth_creds()
        service = build("gmail", "v1", credentials=creds)
        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


agent = VeilleAgent()
