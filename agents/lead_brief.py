"""
agents/lead_brief.py — Pipeline d'automatisation Agent Brief Welldone.

Quand un nouveau lead arrive via le formulaire (Statut = "Nouveau" dans la DB Notion),
ce worker prend le relais et exécute tout le pipeline jusqu'à l'email final
avec lien HTML partageable, sans intervention humaine.

Pipeline :
  1. Lock atomique Notion (Statut: Nouveau → En cours par {worker_id})
  2. Fetch lead complet
  3. Crawl site client (Firecrawl) + extraction couleur dominante
  4. Recherche concurrentielle (Claude API avec web_search)
  5. Synthèse brief markdown (Claude API)
  6. Création sous-page Notion "Brief — {client} — {date}"
  7. Génération HTML pitch (template Jinja2 + accent custom)
  8. Deploy Netlify (API)
  9. POST notify-brief enrichi avec lien HTML
  10. Update Statut → "À analyser"
  11. (optionnel) iMessage notif via webhook Mac local

Erreurs → Telegram notifier (core.telegram_notifier).
Idempotence assurée par le lock Notion : si un autre worker (Mac) a pris le job,
celui-ci abort proprement sans dupliquer le travail.

Endpoint d'invocation :
  POST /run/lead_brief/process
    body: {"sujet": null, "context": {"page_id": "<notion-page-uuid>"}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

from agents._base import BaseAgent

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATA_SOURCE_ID = os.environ.get(
    "NOTION_DATA_SOURCE_ID",
    "2631e626-cff7-4cc0-8367-70843aabd754",  # Demandes marketing — Welldone Studio
)
NOTION_VERSION = "2025-09-03"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("LEAD_BRIEF_MODEL", "claude-opus-4-5-20251101")

NETLIFY_TOKEN = os.environ.get("NETLIFY_TOKEN", "")
NETLIFY_SITE_ID = os.environ.get(
    "NETLIFY_SITE_ID_PITCHES",
    "5f7ce3df-c980-41bf-a7b9-d4ae9d7de605",  # welldone-formulaire-socle
)

NOTIFY_BRIEF_URL = os.environ.get(
    "NOTIFY_BRIEF_URL",
    "https://formulaire.awelldone.studio/.netlify/functions/notify-brief",
)
NOTIFY_BRIEF_SECRET = os.environ.get("NOTIFY_BRIEF_SECRET", "")

IMESSAGE_WEBHOOK_URL = os.environ.get("IMESSAGE_WEBHOOK_URL", "")  # Mac local via Cloudflare Tunnel
IMESSAGE_WEBHOOK_SECRET = os.environ.get("IMESSAGE_WEBHOOK_SECRET", "")

WORKER_ID = os.environ.get("WORKER_ID", f"railway-{os.environ.get('RAILWAY_GIT_COMMIT_SHA', 'local')[:7]}")

ACCENT_DEFAULT = "#ff8a3d"  # Orange Welldone par défaut

# Path du template HTML — embarqué dans l'image Docker
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "pitch_template.html"

# ─────────────────────────────────────────────────────────────────────────
# Helpers — Notion API (urllib stdlib pour éviter dépendances)
# ─────────────────────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_req(method: str, path: str, data: dict | None = None, timeout: int = 15) -> dict:
    url = f"https://api.notion.com/v1/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=_notion_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"Notion {method} {path} → HTTP {e.code}: {detail}") from e


def _fetch_lead(page_id: str) -> dict:
    """Récupère la page Notion complète et extrait les propriétés en dict plat."""
    page = _notion_req("GET", f"pages/{page_id}")
    props = page.get("properties", {})

    def _txt(key: str) -> str:
        prop = props.get(key, {})
        t = prop.get("type")
        if t == "title":
            return "".join(x.get("plain_text", "") for x in prop.get("title", []))
        if t == "rich_text":
            return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
        if t == "select":
            sel = prop.get("select")
            return sel.get("name", "") if sel else ""
        if t == "email":
            return prop.get("email", "") or ""
        if t == "phone_number":
            return prop.get("phone_number", "") or ""
        if t == "url":
            return prop.get("url", "") or ""
        if t == "number":
            n = prop.get("number")
            return str(n) if n is not None else ""
        return ""

    return {
        "page_id": page["id"],
        "page_url": page.get("url", ""),
        "nom": _txt("Nom"),
        "entreprise": _txt("Entreprise"),
        "courriel": _txt("Courriel"),
        "telephone": _txt("Téléphone"),
        "site": _txt("Site web"),
        "objectif": _txt("Objectif principal"),
        "besoin": _txt("Besoin principal"),
        "resultat": _txt("Résultat recherché"),
        "horizon": _txt("Horizon de résultat"),
        "budget_total": _txt("Budget marketing total"),
        "budget_promo": _txt("Budget promotion"),
        "priorite": _txt("Priorité exprimée"),
        "enjeu": _txt("Enjeu si rien ne change"),
        "score_ambition": int(_txt("Score ambition") or 0),
        "score_capacite": int(_txt("Score capacité") or 0),
        "ecart": int(_txt("Écart ambition-capacité") or 0),
        "coherence": _txt("Cohérence"),
        "forfait_formulaire": _txt("Forfait suggéré"),
        "statut": _txt("Statut"),
    }


def _try_lock(page_id: str) -> bool:
    """
    Lock atomique : update Statut "Nouveau" → "En cours par {WORKER_ID}".

    Notion ne supporte pas les updates conditionnels nativement, donc on fait
    un read-then-write avec une fenêtre courte. Le risque de race entre Mac et
    Railway est minime mais existe — on le réduit avec un check post-update.
    """
    # 1. Lecture du statut actuel
    page = _notion_req("GET", f"pages/{page_id}")
    statut = page.get("properties", {}).get("Statut", {}).get("select", {})
    current = statut.get("name", "") if statut else ""
    if current != "Nouveau":
        log.info(f"lead_brief.lock: page {page_id[:8]} déjà {current!r} — skip")
        return False

    # 2. Update Statut
    new_statut = f"En cours ({WORKER_ID})"
    try:
        _notion_req("PATCH", f"pages/{page_id}", {
            "properties": {"Statut": {"select": {"name": new_statut}}}
        })
    except RuntimeError as e:
        log.warning(f"lead_brief.lock: update échoué — {e}")
        return False

    # 3. Re-lecture pour confirmer (anti-race)
    page2 = _notion_req("GET", f"pages/{page_id}")
    statut2 = page2.get("properties", {}).get("Statut", {}).get("select", {})
    confirmed = statut2.get("name", "") if statut2 else ""
    if confirmed != new_statut:
        log.warning(f"lead_brief.lock: race detected — observed {confirmed!r}, expected {new_statut!r}")
        return False

    log.info(f"lead_brief.lock: ✅ acquis sur {page_id[:8]} par {WORKER_ID}")
    return True


def _set_status(page_id: str, status: str) -> None:
    _notion_req("PATCH", f"pages/{page_id}", {
        "properties": {"Statut": {"select": {"name": status}}}
    })


def _create_subpage_brief(parent_page_id: str, title: str, markdown_content: str) -> dict:
    """
    Crée une sous-page Notion sous la fiche lead avec le brief en contenu.
    Convertit le markdown en blocks Notion (heuristique simple).
    """
    blocks = _markdown_to_notion_blocks(markdown_content)

    page = _notion_req("POST", "pages", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "🎯"},
        "properties": {
            "title": [{"type": "text", "text": {"content": title}}]
        },
        "children": blocks[:100],  # Notion limite à 100 blocks par création
    })

    # Si plus de 100 blocks, append le reste
    page_id = page["id"]
    remaining = blocks[100:]
    while remaining:
        chunk = remaining[:100]
        remaining = remaining[100:]
        _notion_req("PATCH", f"blocks/{page_id}/children", {"children": chunk})

    return page


def _markdown_to_notion_blocks(md: str) -> list[dict]:
    """Conversion heuristique markdown → Notion blocks. Suffisant pour le brief."""
    blocks = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Headers
        if stripped.startswith("# "):
            blocks.append(_block("heading_1", stripped[2:]))
        elif stripped.startswith("## "):
            blocks.append(_block("heading_2", stripped[3:]))
        elif stripped.startswith("### "):
            blocks.append(_block("heading_3", stripped[4:]))
        # Quote
        elif stripped.startswith("> "):
            blocks.append(_block("quote", stripped[2:]))
        # Bullet list
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(_block("bulleted_list_item", stripped[2:]))
        # Numbered list
        elif re.match(r"^\d+\.\s", stripped):
            content = re.sub(r"^\d+\.\s", "", stripped)
            blocks.append(_block("numbered_list_item", content))
        # Code block
        elif stripped.startswith("```"):
            lang = stripped[3:].strip() or "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": _rich_text_from_md("\n".join(code_lines)),
                    "language": lang if lang in _NOTION_LANGS else "plain text",
                },
            })
        # Divider
        elif stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        # Paragraph
        else:
            blocks.append(_block("paragraph", stripped))

        i += 1

    return blocks


def _block(kind: str, content: str) -> dict:
    return {
        "object": "block",
        "type": kind,
        kind: {"rich_text": _rich_text_from_md(content)},
    }


def _rich_text_from_md(text: str) -> list[dict]:
    """Parse markdown bold/italic/code/link en rich_text Notion."""
    if not text:
        return []
    # Découpage simple par segments — gère **bold**, *italic*, `code`, [text](url)
    pattern = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
    pieces = pattern.split(text)
    out = []
    for p in pieces:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**"):
            out.append({"type": "text", "text": {"content": p[2:-2]}, "annotations": {"bold": True}})
        elif p.startswith("*") and p.endswith("*") and len(p) > 2:
            out.append({"type": "text", "text": {"content": p[1:-1]}, "annotations": {"italic": True}})
        elif p.startswith("`") and p.endswith("`"):
            out.append({"type": "text", "text": {"content": p[1:-1]}, "annotations": {"code": True}})
        elif p.startswith("[") and "](" in p:
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", p)
            if m:
                out.append({"type": "text", "text": {"content": m.group(1), "link": {"url": m.group(2)}}})
            else:
                out.append({"type": "text", "text": {"content": p}})
        else:
            # Notion limit : 2000 chars per rich_text item
            for chunk in [p[i:i+2000] for i in range(0, len(p), 2000)]:
                out.append({"type": "text", "text": {"content": chunk}})
    return out


_NOTION_LANGS = {
    "python", "javascript", "typescript", "html", "css", "bash", "shell",
    "json", "markdown", "plain text", "sql", "yaml",
}

# ─────────────────────────────────────────────────────────────────────────
# Helpers — Crawl site + extraction couleur
# ─────────────────────────────────────────────────────────────────────────

def _crawl_site(url: str) -> dict:
    """
    Crawle la home page du lead via Firecrawl.
    Retourne {markdown, html, links, screenshot_url} ou {} si échec.
    """
    if not url or not url.startswith("http"):
        return {}
    try:
        from firecrawl import FirecrawlApp
        api_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not api_key:
            log.warning("lead_brief.crawl: FIRECRAWL_API_KEY absent — skip")
            return {}
        fc = FirecrawlApp(api_key=api_key)
        result = fc.scrape_url(url, params={"formats": ["markdown", "html"]})
        return {
            "markdown": result.get("markdown", "")[:8000],
            "html": result.get("html", "")[:50000],
            "metadata": result.get("metadata", {}),
        }
    except Exception as e:
        log.warning(f"lead_brief.crawl: {url} → {e}")
        return {}


def _extract_accent_color(crawl_result: dict) -> str:
    """
    Extrait la couleur d'accent du site crawlé.

    Heuristique :
      1. Cherche les variables CSS --accent / --primary / --brand dans le HTML
      2. Sinon parse les couleurs hex/rgb dans les styles inline
      3. Filtre : saturation >40%, contraste ok sur fond noir
      4. Si aucune dominante claire → orange Welldone par défaut
    """
    html = crawl_result.get("html", "")
    if not html:
        return ACCENT_DEFAULT

    # 1. Variables CSS prioritaires
    for var in ["--accent", "--primary", "--brand", "--accent-color"]:
        m = re.search(rf"{re.escape(var)}\s*:\s*(#[0-9a-fA-F]{{3,8}}|rgb[a]?\([^)]+\))", html)
        if m:
            color = m.group(1).strip()
            if _is_usable_on_dark(color):
                return _normalize_hex(color)

    # 2. Compte les couleurs hex utilisées
    hexes = re.findall(r"#[0-9a-fA-F]{6}\b", html)
    counts: dict[str, int] = {}
    for h in hexes:
        h = h.lower()
        if _is_neutral(h):  # ignore noir/blanc/gris
            continue
        if not _is_usable_on_dark(h):
            continue
        counts[h] = counts.get(h, 0) + 1

    if counts:
        # Top color qui apparaît au moins 3 fois
        top = sorted(counts.items(), key=lambda x: -x[1])
        if top[0][1] >= 3:
            return top[0][0]

    return ACCENT_DEFAULT


def _is_neutral(hex_color: str) -> bool:
    """True si la couleur est noir/blanc/gris (faible saturation)."""
    try:
        r, g, b = _hex_to_rgb(hex_color)
    except ValueError:
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    saturation = (mx - mn) / 255 if mx else 0
    return saturation < 0.20


def _is_usable_on_dark(color: str) -> bool:
    """True si la couleur a un contraste suffisant sur fond #0a0a0c."""
    try:
        if color.startswith("rgb"):
            nums = re.findall(r"\d+", color)
            r, g, b = int(nums[0]), int(nums[1]), int(nums[2])
        else:
            r, g, b = _hex_to_rgb(color)
    except (ValueError, IndexError):
        return False
    # Luminance relative simplifiée
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return luminance > 0.35  # assez clair pour être lisible sur dark


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"hex invalide: {hex_color}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _normalize_hex(color: str) -> str:
    if color.startswith("#"):
        return color.lower()
    if color.startswith("rgb"):
        nums = re.findall(r"\d+", color)
        if len(nums) >= 3:
            r, g, b = int(nums[0]), int(nums[1]), int(nums[2])
            return f"#{r:02x}{g:02x}{b:02x}"
    return ACCENT_DEFAULT


# ─────────────────────────────────────────────────────────────────────────
# Helpers — Anthropic API (synthèse brief + recherche)
# ─────────────────────────────────────────────────────────────────────────

def _generate_brief_content(lead: dict, crawl_result: dict) -> dict:
    """
    Appel Claude API pour générer le brief structuré.

    Retourne un dict avec les sections nécessaires au markdown Notion ET
    aux données structurées du HTML pitch (3 stats clés, 3 piliers, plans A/B,
    titre punchline, etc).
    """
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    site_summary = crawl_result.get("markdown", "")[:4000] if crawl_result else ""

    system_prompt = """Tu es l'analyste pré-RDV de Welldone Studio. Tu produis des briefs commerciaux DOCUMENTÉS pour les leads marketing entrants.

Règle absolue : aucune généralité. Chaque affirmation cite une observation concrète (URL, chiffre, citation). Si tu ne peux pas étayer, dis-le ("Donnée non trouvée — à valider en RDV").

Tu utilises web_search pour la recherche concurrentielle locale (Montréal sauf indication contraire).

Format de sortie : JSON strict avec ces clés :
- "punchline": str (1 phrase de couverture, max 12 mots, avec un mot/groupe en accent ENTRE **étoiles**)
- "tagline": str (2-3 phrases sous-titre)
- "vote_scenario": str ("A" | "B" | "C")
- "vote_forfait": str (ex: "Welldone Sur mesure 5 000 $/mois + setup 4 500 $")
- "promesse_mesurable": str (1-2 phrases, chiffres concrets)
- "diagnostic_ligne": str (1 phrase, source citée)
- "question_creuser": str (1 question critique pour le RDV)
- "stats": [{"value": "0/9", "label": "Concurrents 3 segments", "explain": "...", "featured": true}, ...4 stats max]
- "piliers": [{"num": "01", "title": "Déployer", "desc": "..."}, ...3 piliers]
- "plan_a": {"name": "...", "subtitle": "...", "vagues": [{"label": "Vague 0", "duration": "Onboarding", "when": "Semaine 1-2 · 1 500 $", "headline": "...", "actions": ["...", "..."]}, ...4 vagues], "metric": "..."}
- "plan_b": {même structure}
- "onboarding_amount": str (ex: "1 500 $")
- "onboarding_items": [{"icon": "1", "title": "...", "desc": "..."}, ...4 items]
- "engagement_ecrit": str (1 paragraphe garantie)
- "brief_markdown": str (le brief Notion complet en markdown, sections : Synthèse exécutive, Diagnostic, Lecture croisée, 3 scénarios, Vote, Préparation RDV, Brouillon pitch, Métadonnées)

Le brief_markdown doit faire 800-1500 mots, suivre la structure du skill welldone-agent-brief, et être aussi rigoureux que possible.
"""

    user_prompt = f"""# Lead à analyser

## Données formulaire
- Nom : {lead['nom']}
- Entreprise : {lead['entreprise']}
- Courriel : {lead['courriel']}
- Téléphone : {lead['telephone'] or 'non communiqué'}
- Site : {lead['site'] or 'non communiqué'}
- Objectif : {lead['objectif']}
- Besoin : {lead['besoin']}
- Résultat recherché : {lead['resultat']}
- Horizon : {lead['horizon']}
- Budget total : {lead['budget_total']}
- Budget promo : {lead['budget_promo']}
- Priorité exprimée : {lead['priorite']}
- Enjeu si rien ne change : {lead['enjeu'] or 'non communiqué'}
- Score ambition : {lead['score_ambition']}
- Score capacité : {lead['score_capacite']}
- Écart : {lead['ecart']}
- Cohérence : {lead['coherence']}
- Forfait formulaire : {lead['forfait_formulaire']}

## Site web (extrait crawl Firecrawl)
{site_summary or '(site non communiqué ou crawl échoué — diagnostic limité)'}

## Mission
1. Recherche les 5-9 concurrents directs locaux via web_search
2. Identifie 2-3 désalignements formulaire vs réalité observée
3. Identifie 2-4 opportunités cachées
4. Construis 2 scénarios (A recommandé · B alternative)
5. Vote pour A et argumente
6. Produis le JSON de sortie complet

Date du brief : {datetime.now().strftime('%Y-%m-%d')}.
Sortie JSON UNIQUEMENT, pas de texte avant ou après."""

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        )
        # Extraction du JSON de la dernière réponse texte
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        # Nettoyage : enlever les ```json ... ``` éventuels
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        log.error(f"lead_brief.generate: {e}")
        # Fallback minimal pour ne pas casser le pipeline
        return _fallback_brief_content(lead)


def _fallback_brief_content(lead: dict) -> dict:
    """Contenu minimal si l'API Claude échoue. Permet quand même d'envoyer un email."""
    return {
        "punchline": f"Lead reçu : {lead['entreprise']}.",
        "tagline": "Brief automatique partiel — diagnostic complet à faire manuellement.",
        "vote_scenario": "C",
        "vote_forfait": lead["forfait_formulaire"],
        "promesse_mesurable": "À calibrer après diagnostic complet.",
        "diagnostic_ligne": f"Lead {lead['coherence'].lower()} — ambition {lead['score_ambition']}, capacité {lead['score_capacite']}.",
        "question_creuser": "Diagnostic auto incomplet — questions à creuser à définir manuellement.",
        "stats": [
            {"value": str(lead["score_ambition"]), "label": "Score ambition", "explain": "Du formulaire", "featured": True},
            {"value": str(lead["score_capacite"]), "label": "Score capacité", "explain": "Du formulaire"},
        ],
        "piliers": [
            {"num": "01", "title": "Diagnostic", "desc": "À compléter en RDV."},
            {"num": "02", "title": "Plan", "desc": "À calibrer."},
            {"num": "03", "title": "Exécution", "desc": "À cadrer."},
        ],
        "plan_a": {
            "name": "Plan recommandé",
            "subtitle": "À détailler en RDV.",
            "vagues": [
                {"label": "Vague 0", "duration": "Onboarding", "when": "Semaine 1-2", "headline": "Audit + roadmap", "actions": ["Workshop kick-off"]},
            ],
            "metric": "À définir",
        },
        "plan_b": {
            "name": "Plan alternative",
            "subtitle": "Si périmètre réduit.",
            "vagues": [
                {"label": "Vague 0", "duration": "Onboarding", "when": "Semaine 1-2", "headline": "Audit léger", "actions": ["Workshop court"]},
            ],
            "metric": "À définir",
        },
        "onboarding_amount": "1 500 $",
        "onboarding_items": [
            {"icon": "1", "title": "Audit", "desc": "Diagnostic complet"},
            {"icon": "2", "title": "Roadmap", "desc": "Plan personnalisé"},
            {"icon": "3", "title": "Setup", "desc": "Tracking + outils"},
            {"icon": "4", "title": "Calibrage", "desc": "Mensuel adapté"},
        ],
        "engagement_ecrit": "Engagement à définir.",
        "brief_markdown": f"# Brief — {lead['entreprise']}\n\n*Brief auto incomplet — Claude API non disponible. Compléter manuellement.*\n\n## Lead snapshot\n\n- Nom : {lead['nom']}\n- Score : {lead['score_ambition']}/{lead['score_capacite']} (écart {lead['ecart']})\n- Cohérence : {lead['coherence']}\n",
    }


# ─────────────────────────────────────────────────────────────────────────
# Helpers — Génération HTML pitch via template Jinja2
# ─────────────────────────────────────────────────────────────────────────

def _render_pitch_html(lead: dict, brief: dict, accent_color: str) -> str:
    """Charge le template Jinja2 et le remplit avec les données du brief."""
    try:
        from jinja2 import Template
    except ImportError:
        log.error("lead_brief.render: jinja2 absent du requirements.txt")
        raise

    if not _TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template manquant : {_TEMPLATE_PATH}")

    template_src = _TEMPLATE_PATH.read_text(encoding="utf-8")
    template = Template(template_src, autoescape=False)

    # Calcul de l'accent secondaire (lighten)
    accent_bright = _lighten_hex(accent_color, 0.25)

    return template.render(
        client_nom=lead["nom"],
        client_entreprise=lead["entreprise"],
        date_brief=datetime.now().strftime("%-d %B %Y"),
        accent=accent_color,
        accent_bright=accent_bright,
        accent_rgb=",".join(str(c) for c in _hex_to_rgb(accent_color)),
        punchline=brief["punchline"],
        tagline=brief["tagline"],
        promesse=brief["promesse_mesurable"],
        stats=brief["stats"],
        piliers=brief["piliers"],
        plan_a=brief["plan_a"],
        plan_b=brief["plan_b"],
        onboarding_amount=brief["onboarding_amount"],
        onboarding_items=brief["onboarding_items"],
        engagement=brief["engagement_ecrit"],
    )


def _lighten_hex(hex_color: str, amount: float) -> str:
    """Éclaircit une couleur hex de `amount` (0-1) vers le blanc."""
    try:
        r, g, b = _hex_to_rgb(hex_color)
    except ValueError:
        return hex_color
    r = min(255, int(r + (255 - r) * amount))
    g = min(255, int(g + (255 - g) * amount))
    b = min(255, int(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def _slugify(text: str) -> str:
    """Slugifie un nom d'entreprise pour le filename HTML."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")[:60] or "lead"


# ─────────────────────────────────────────────────────────────────────────
# Helpers — Deploy Netlify via API
# ─────────────────────────────────────────────────────────────────────────

def _deploy_to_netlify(html_content: str, filename: str) -> str:
    """
    Upload le HTML vers Netlify via l'API Deploys.
    Retourne l'URL publique du fichier déployé (draft).

    Approche : POST un nouveau deploy avec un seul fichier.
    """
    if not NETLIFY_TOKEN:
        log.warning("lead_brief.deploy: NETLIFY_TOKEN absent — skip deploy")
        return ""

    import hashlib
    sha1 = hashlib.sha1(html_content.encode()).hexdigest()
    file_path = f"/p/{filename}"

    # 1. Créer un draft deploy
    create_url = f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys"
    headers = {
        "Authorization": f"Bearer {NETLIFY_TOKEN}",
        "Content-Type": "application/json",
    }
    body = json.dumps({
        "files": {file_path: sha1},
        "draft": True,
        "title": f"lead_brief: {filename}",
    }).encode()

    req = urllib.request.Request(create_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            deploy = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"Netlify create deploy failed: {e.code} {detail}") from e

    deploy_id = deploy["id"]
    required = deploy.get("required", [])

    # 2. Upload du fichier si requis
    if sha1 in required:
        upload_url = f"https://api.netlify.com/api/v1/deploys/{deploy_id}/files{file_path}"
        upload_headers = {
            "Authorization": f"Bearer {NETLIFY_TOKEN}",
            "Content-Type": "application/octet-stream",
        }
        upload_req = urllib.request.Request(
            upload_url, data=html_content.encode(), headers=upload_headers, method="PUT"
        )
        with urllib.request.urlopen(upload_req, timeout=30) as resp:
            resp.read()

    # 3. Retourner l'URL draft du deploy
    deploy_url = deploy.get("deploy_ssl_url") or deploy.get("deploy_url") or ""
    return f"{deploy_url}{file_path}" if deploy_url else ""


# ─────────────────────────────────────────────────────────────────────────
# Helpers — Notify (email Resend via Netlify Function + iMessage)
# ─────────────────────────────────────────────────────────────────────────

def _post_notify_brief(payload: dict) -> bool:
    """POST vers la Netlify Function notify-brief enrichie avec url_pitch_html."""
    headers = {"Content-Type": "application/json"}
    if NOTIFY_BRIEF_SECRET:
        headers["X-Notify-Secret"] = NOTIFY_BRIEF_SECRET

    body = json.dumps(payload).encode()
    req = urllib.request.Request(NOTIFY_BRIEF_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            response = json.loads(resp.read())
            return response.get("success", False)
    except Exception as e:
        log.error(f"lead_brief.notify: {e}")
        return False


def _post_imessage(message: str) -> bool:
    """Post vers le webhook Mac local (Cloudflare Tunnel) pour envoyer un iMessage."""
    if not IMESSAGE_WEBHOOK_URL:
        log.info("lead_brief.imessage: pas de webhook configuré — skip")
        return False
    headers = {"Content-Type": "application/json"}
    if IMESSAGE_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = IMESSAGE_WEBHOOK_SECRET
    body = json.dumps({"message": message}).encode()
    req = urllib.request.Request(IMESSAGE_WEBHOOK_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
            return True
    except Exception as e:
        log.warning(f"lead_brief.imessage: Mac unreachable ({e}) — silently skip")
        return False


# ─────────────────────────────────────────────────────────────────────────
# Pipeline complet
# ─────────────────────────────────────────────────────────────────────────

async def _run_pipeline(page_id: str) -> str:
    """Exécute le pipeline complet pour un lead. Retourne un message de status."""
    started = time.time()

    # Étape 1 : Lock atomique
    if not _try_lock(page_id):
        return f"⏭ Lead {page_id[:8]} déjà pris en charge par un autre worker — skip"

    try:
        # Étape 2 : Fetch lead
        lead = _fetch_lead(page_id)
        log.info(f"lead_brief: traitement de {lead['entreprise']} ({lead['nom']})")

        # Étape 3 : Crawl site + extraction couleur
        crawl_result = _crawl_site(lead["site"])
        accent = _extract_accent_color(crawl_result)
        log.info(f"lead_brief: accent détecté = {accent}")

        # Étape 4-5 : Génération brief (recherche + synthèse)
        brief = _generate_brief_content(lead, crawl_result)

        # Étape 6 : Création sous-page Notion
        date_str = datetime.now().strftime("%Y-%m-%d")
        title = f"Brief — {lead['entreprise']} · {lead['nom']} — {date_str}"
        subpage = _create_subpage_brief(page_id, title, brief["brief_markdown"])
        url_brief_notion = subpage.get("url", "")

        # Étape 7 : Génération HTML
        slug = _slugify(f"{lead['entreprise']}-{lead['nom']}")
        html_filename = f"{slug}.html"
        html_content = _render_pitch_html(lead, brief, accent)

        # Étape 8 : Deploy Netlify
        url_pitch_html = _deploy_to_netlify(html_content, html_filename)
        log.info(f"lead_brief: HTML déployé → {url_pitch_html or '(failed)'}")

        # Étape 9 : POST notify-brief enrichi
        ville = ""  # à enrichir si dispo dans le crawl
        notify_payload = {
            "nom": lead["nom"],
            "entreprise": lead["entreprise"],
            "courriel_lead": lead["courriel"],
            "telephone": lead["telephone"],
            "site_lead": lead["site"],
            "ville": ville,
            "score_ambition": lead["score_ambition"],
            "score_capacite": lead["score_capacite"],
            "ecart": lead["ecart"],
            "coherence": lead["coherence"],
            "horizon": lead["horizon"],
            "forfait_formulaire": lead["forfait_formulaire"],
            "enjeu_si_rien_change": lead["enjeu"],
            "scenario_voted_name": brief.get("plan_a", {}).get("name", "Scénario A"),
            "forfait_voted": brief["vote_forfait"],
            "scenario_promesse": brief["promesse_mesurable"],
            "diagnostic_ligne": brief["diagnostic_ligne"],
            "question_creuser": brief["question_creuser"],
            "url_brief": url_brief_notion,
            "url_lead": lead["page_url"],
            "url_database": "https://www.notion.so/bece73b69906414c9102f5fd03e0323a",
            "url_pitch_html": url_pitch_html,
            "accent_color": accent,
        }
        notify_ok = _post_notify_brief(notify_payload)

        # Étape 10 : Update Statut Notion
        _set_status(page_id, "À analyser")

        # Étape 11 : iMessage notification (best-effort)
        imessage_text = (
            f"🎯 Brief {lead['entreprise']} prêt\n"
            f"Vote : {brief['vote_forfait']}\n"
            f"☎️ {lead['telephone'] or lead['courriel']}\n"
            f"👉 {url_pitch_html}"
        )
        _post_imessage(imessage_text)

        elapsed = int(time.time() - started)
        return (
            f"✅ Brief {lead['entreprise']} traité en {elapsed}s\n"
            f"   • Sous-page Notion : {url_brief_notion}\n"
            f"   • HTML pitch : {url_pitch_html or '(deploy failed)'}\n"
            f"   • Email envoyé : {notify_ok}\n"
            f"   • Worker : {WORKER_ID}"
        )

    except Exception as e:
        # Reset le statut pour permettre un retry manuel
        try:
            _set_status(page_id, "Échec")
        except Exception:
            pass

        # Notifier sur Telegram
        try:
            from core.telegram_notifier import notify
            await notify(
                f"🚨 *Erreur Agent Brief auto*\n"
                f"Lead : `{page_id[:8]}`\n"
                f"Worker : `{WORKER_ID}`\n"
                f"Erreur : `{str(e)[:300]}`\n\n"
                f"→ Statut Notion remis à *Échec* — retry manuel possible."
            )
        except Exception:
            pass

        log.exception(f"lead_brief.pipeline: erreur sur {page_id}")
        raise


# ─────────────────────────────────────────────────────────────────────────
# Classe Agent (interface BaseAgent)
# ─────────────────────────────────────────────────────────────────────────

class LeadBriefAgent(BaseAgent):
    name = "lead_brief"
    description = "Pipeline auto : formulaire → brief Notion + HTML pitch + email avec lien"

    @property
    def commands(self):
        return {"process": self.process}

    async def process(self, context: dict | None = None) -> str:
        ctx = context or {}
        page_id = ctx.get("page_id") or ctx.get("lead_id") or ctx.get("sujet", "")
        if not page_id:
            return "❌ Paramètre 'page_id' manquant dans le context"

        # Normalise (avec ou sans tirets)
        page_id = page_id.replace("-", "")
        if len(page_id) == 32:
            page_id = f"{page_id[0:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:32]}"

        return await _run_pipeline(page_id)


# Auto-instanciation pour le dispatcher
agent = LeadBriefAgent()
