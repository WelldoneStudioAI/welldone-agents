"""
core/notion_delivery.py — Pipeline Notion pour les outputs IA.

Deux fonctions publiques :
  pipeline_create(title, agent, type_, content, framer_url, status)
    → Page complète dans "Pipeline Agents IA" (reviseur, analytics, veille)
    → Retourne l'URL Notion (str) ou None si échec

  pipeline_log(title, agent, framer_url, notes)
    → Entrée légère sans contenu riche (blog, framer — trace minimale)
    → Retourne l'URL Notion (str) ou None si échec

0 appels Claude. Uniquement API Notion directe (urllib). Timeout: 10s.
"""
import asyncio
import json
import logging
import urllib.request

from core.auth import get_notion_headers
from config import NOTION_PIPELINE_DB

log = logging.getLogger(__name__)


def _notion_post(path: str, data: dict) -> dict:
    """Appel HTTP POST vers l'API Notion (synchrone)."""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=body,
        headers=get_notion_headers(),
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _content_to_blocks(content: str) -> list:
    """Convertit un texte brut en blocs paragraphe Notion (max 2000 chars chacun)."""
    if not content:
        return []
    # Limiter à ~50 000 chars pour éviter les timeouts Notion (≈ 25 blocs max)
    chunks = [content[i : i + 2000] for i in range(0, min(len(content), 50000), 2000)]
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        }
        for chunk in chunks
    ]


async def pipeline_create(
    title: str,
    agent: str,
    type_: str,
    content: str,
    framer_url: str | None = None,
    status: str = "Prêt révision",
) -> str | None:
    """
    Crée une page complète dans le Pipeline Agents IA Notion.
    Usage : reviseur, analytics, veille (contenu riche).

    Args:
        title     : Titre de la page (ex: "Révision — mon-article-slug")
        agent     : Nom de l'agent source (ex: "reviseur", "analytics", "veille")
        type_     : Type de livrable (ex: "révision", "rapport", "veille")
        content   : Contenu complet en texte brut
        framer_url: URL Framer staging ou externe (optionnel)
        status    : Statut initial dans le pipeline ("En cours" | "Prêt révision" | ...)

    Returns:
        URL Notion de la page créée, ou None si NOTION_PIPELINE_DB absent ou erreur.
    """
    db_id = NOTION_PIPELINE_DB
    if not db_id:
        log.warning("notion_delivery: NOTION_PIPELINE_DB non définie — skip")
        return None

    props: dict = {
        "Nom":         {"title": [{"text": {"content": title[:200]}}]},
        "Statut":      {"select": {"name": status}},
        "Agent":       {"select": {"name": agent[:50]}},
        "Type":        {"select": {"name": type_[:50]}},
        "Créé par IA": {"checkbox": True},
    }
    if framer_url:
        props["Lien"] = {"url": framer_url}

    payload: dict = {
        "parent":     {"database_id": db_id},
        "properties": props,
        "children":   _content_to_blocks(content),
    }

    try:
        page = await asyncio.to_thread(_notion_post, "pages", payload)
        url  = page.get("url", "")
        log.info(f"notion_delivery.pipeline_create: {agent}/{type_} → {url}")
        return url or None
    except Exception as e:
        log.error(f"notion_delivery.pipeline_create error: {e}")
        return None


async def pipeline_log(
    title: str,
    agent: str,
    framer_url: str | None = None,
    notes: str | None = None,
) -> str | None:
    """
    Entrée légère dans le Pipeline Agents IA (sans contenu riche).
    Usage : blog, framer (trace minimale — l'output va dans Framer staging).

    Args:
        title     : Titre de l'article ou livrable
        agent     : Nom de l'agent source (ex: "framer", "blog")
        framer_url: URL Framer staging
        notes     : Notes courtes (optionnel, max ~2000 chars)

    Returns:
        URL Notion de l'entrée créée, ou None si erreur.
    """
    db_id = NOTION_PIPELINE_DB
    if not db_id:
        log.warning("notion_delivery: NOTION_PIPELINE_DB non définie — skip")
        return None

    props: dict = {
        "Nom":         {"title": [{"text": {"content": title[:200]}}]},
        "Statut":      {"select": {"name": "Publié"}},
        "Agent":       {"select": {"name": agent[:50]}},
        "Type":        {"select": {"name": "trace"}},
        "Créé par IA": {"checkbox": True},
    }
    if framer_url:
        props["Lien"] = {"url": framer_url}

    payload: dict = {
        "parent":     {"database_id": db_id},
        "properties": props,
    }
    if notes:
        payload["children"] = _content_to_blocks(notes[:2000])

    try:
        page = await asyncio.to_thread(_notion_post, "pages", payload)
        url  = page.get("url", "")
        log.info(f"notion_delivery.pipeline_log: {agent} → {url}")
        return url or None
    except Exception as e:
        log.error(f"notion_delivery.pipeline_log error: {e}")
        return None
