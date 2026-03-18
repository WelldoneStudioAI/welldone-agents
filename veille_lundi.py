#!/usr/bin/env python3
"""
Veille Welldone — Étape 1 (Lundi 8h)
- Lit les sources depuis Notion
- Parse les flux RSS
- Génère 10+ idées d'articles avec Claude
- Crée une page Notion de sélection (checkboxes)
- Envoie un email avec la liste + lien Notion
"""

import os, warnings, json
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta
import feedparser
import anthropic
import urllib.request as _urllib_req
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_TOKEN        = os.environ.get("NOTION_TOKEN", "")
NOTION_SOURCES_DB   = os.environ.get("NOTION_SOURCES_DB", "10e87f01b8ec4c60a6383d48c7aac4a2")
NOTION_SELECTION_DB = os.environ.get("NOTION_SELECTION_DB", "")
GMAIL_TOKEN         = os.path.expanduser(os.environ.get("GMAIL_TOKEN", "~/.config/gws/gmail_send_token.json"))
GMAIL_RECIPIENT     = os.environ.get("GMAIL_RECIPIENT", "awelldonestudio@gmail.com")

TODAY = datetime.today().strftime("%d %B %Y")
SEMAINE = (datetime.today() - timedelta(days=7)).strftime("%d %B")


# ── Notion API helper ─────────────────────────────────────────────────────────
def notion_req(method, path, data=None):
    import json as _json
    req = _urllib_req.Request(
        f"https://api.notion.com/v1/{path}",
        data=_json.dumps(data or {}).encode(),
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
        method=method
    )
    resp = _urllib_req.urlopen(req, timeout=10)
    return _json.loads(resp.read())


# ── 1. Lire les sources depuis Notion ─────────────────────────────────────────
def get_sources():
    results = notion_req("POST", f"databases/{NOTION_SOURCES_DB}/query",
                         {"filter": {"property": "Actif", "checkbox": {"equals": True}}})
    sources = []
    for page in results.get("results", []):
        props = page["properties"]
        nom = props["Nom"]["title"][0]["plain_text"] if props["Nom"]["title"] else ""
        rss = (props["RSS"]["url"] or "") if props.get("RSS") else ""
        url = (props["URL"]["url"] or "") if props.get("URL") else ""
        categorie = props["Catégorie"]["select"]["name"] if props["Catégorie"]["select"] else ""
        langue = props["Langue"]["select"]["name"] if props["Langue"]["select"] else ""
        if rss:
            sources.append({"nom": nom, "rss": rss, "url": url, "categorie": categorie, "langue": langue})
    return sources


# ── 2. Parser les flux RSS ─────────────────────────────────────────────────────
def fetch_articles(sources, max_per_source=4):
    articles = []
    for src in sources:
        try:
            feed = feedparser.parse(src["rss"])
            for entry in feed.entries[:max_per_source]:
                published = entry.get("published", "")[:10] if entry.get("published") else ""
                summary = entry.get("summary", entry.get("description", ""))[:500]
                articles.append({
                    "titre": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "resume": summary,
                    "source": src["nom"],
                    "categorie": src["categorie"],
                    "langue": src["langue"],
                    "date": published,
                })
        except Exception as e:
            print(f"  ⚠️  Erreur RSS {src['nom']}: {e}")
    return articles


# ── 3. Générer les idées avec Claude ──────────────────────────────────────────
def generer_idees(articles):
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
- Jamais de traduction directe — réinterprète le sujet avec une perspective québécoise et créative
- Liens avec la photographie commerciale, le branding visuel, la stratégie numérique des PME
- Évite le jargon trop technique — ton accessible et inspirant

Format de réponse : numérote de 1 à 10, structure claire."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ── 4. Créer la page de sélection dans Notion ────────────────────────────────
def creer_page_selection(notion_token, idees_texte):
    titre_page = f"Veille du {TODAY} — Sélection"

    # Découper le texte en blocs (max 2000 chars par bloc)
    chunks = [idees_texte[i:i+1900] for i in range(0, len(idees_texte), 1900)]
    children = [{
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Coche tes 5 favoris avant mardi 8h ↓"}}]}
    }]
    for chunk in chunks:
        children.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}
        })

    page = notion_req("POST", "pages", {
        "parent": {"type": "workspace", "workspace": True},
        "properties": {"title": {"title": [{"text": {"content": titre_page}}]}},
        "children": children
    })
    return page["url"]


# ── 5. Envoyer l'email ────────────────────────────────────────────────────────
def envoyer_email(idees_texte, notion_url):
    creds = Credentials.from_authorized_user_file(
        GMAIL_TOKEN,
        ["https://www.googleapis.com/auth/gmail.send"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    service = build("gmail", "v1", credentials=creds)

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }}
  .container {{ max-width: 680px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; }}
  .header {{ background: #0a0a0a; color: white; padding: 32px 40px; }}
  .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.5px; }}
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
    <p>Voici ta sélection hebdomadaire. J'ai analysé {len(idees_texte.split())} mots de contenu depuis tes 8 sources de référence et généré 10 idées adaptées au marché québécois.</p>
    <a href="{notion_url}" class="cta">Ouvrir dans Notion pour cocher tes 5 choix →</a>
    <div class="idees">{idees_texte}</div>
  </div>
  <div class="footer">
    Welldone Studio · Veille automatisée · Les 5 articles approuvés seront rédigés et envoyés en brouillon Framer mardi matin.
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Veille Welldone — {TODAY} — 10 idées à approuver"
    msg["From"] = GMAIL_RECIPIENT
    msg["To"] = GMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Email envoyé à {GMAIL_RECIPIENT}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'═'*60}")
    print(f"  VEILLE WELLDONE — Lundi {TODAY}")
    print(f"{'═'*60}\n")

    print("📡 Lecture des sources Notion...")
    sources = get_sources()
    print(f"  {len(sources)} sources actives trouvées")

    print("📰 Récupération des articles RSS...")
    articles = fetch_articles(sources)
    print(f"  {len(articles)} articles récupérés")

    print("🧠 Génération des idées avec Claude...")
    idees = generer_idees(articles)

    print("📋 Création de la page de sélection Notion...")
    try:
        notion_url = creer_page_selection(NOTION_TOKEN, idees)
        print(f"  {notion_url}")
    except Exception as e:
        print(f"  ⚠️  Notion page skipped ({e}) — email envoyé sans lien Notion")
        notion_url = ""

    print("📧 Envoi de l'email...")
    envoyer_email(idees, notion_url)

    print(f"\n{'═'*60}")
    print("  ✅ Veille du lundi complète !")
    print(f"  Tu as jusqu'à mardi 8h pour cocher tes 5 choix dans Notion.")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
