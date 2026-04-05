"""
agents/site_crawler.py — Crawler de site web → Markdown Obsidian éditable.

Crawle awelldone.studio et welldone.archi via Firecrawl.
Produit un fichier .md par page dans le vault Obsidian local (05-Marketing/Site-web/).
Permet à d'autres agents (analyse, réécriture, maillage, Framer) de travailler
sur le contenu textuel du site sans passer par Framer directement.

Commandes :
  /site crawl        → crawl awelldone.studio complet
  /site crawl archi  → crawl welldone.archi complet
  /site page <url>   → scrape une URL précise
  /site rapport      → stats du dernier crawl

Déclenchement local (Claude Code) :
  python dispatch.py site crawl
  python dispatch.py site page --url https://awelldone.studio/a-propos
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from agents._base import BaseAgent
from config import (
    FIRECRAWL_API_KEY,
    OBSIDIAN_VAULT_PATH,
    SITE_STUDIO_URL,
    SITE_ARCHI_URL,
)

log = logging.getLogger(__name__)

# Chemin Obsidian cible
_SITE_WEB_DIR = Path(OBSIDIAN_VAULT_PATH) / "05-Marketing" / "Site-web"

# Fallback si Obsidian non accessible (Railway)
_TMP_DIR = Path("/tmp/site-mirror")

# Fichier de persistance du dernier rapport de crawl
_RAPPORT_FILE = _SITE_WEB_DIR / "_crawl-report.md"

# ── Classification des pages par slug ─────────────────────────────────────────
# RÈGLE : les patterns les plus spécifiques en premier
_SLUG_RULES: list[tuple[list[str], str]] = [
    (["archi-works/", "archi/archi-works"],                          "projets"),   # projets welldone.archi
    (["realisation", "projet", "portfolio", "case", "travaux"],      "projets"),   # projets awelldone.studio
    (["archi/archi-news", "archi/news", "journal", "blogue", "article", "blog"], "articles"),
    (["welldone-studio-services", "offre", "consultation"],          "services"),
    (["archi/archi-about", "archi/notre-approche", "a-propos", "about", "equipe", "team"], "pages"),
    (["archi/archi-contact", "contact", "devis", "soumission"],      "pages"),
    (["legal", "confidentialite", "privacy", "conditions"],          "pages"),
]


def _classify_url(url: str) -> str:
    """Retourne le sous-dossier de destination selon l'URL."""
    path = url.rstrip("/").split("//")[-1]
    parts = path.split("/")

    # Racine du site
    if len(parts) <= 1 or (len(parts) == 2 and not parts[1]):
        return "pages"

    slug = "/".join(parts[1:]).lower()
    for keywords, folder in _SLUG_RULES:
        if any(kw in slug for kw in keywords):
            return folder

    return "pages"


def _url_to_filename(url: str) -> str:
    """Convertit une URL en nom de fichier stable et lisible."""
    path = url.rstrip("/").split("//")[-1]
    parts = path.split("/")
    if len(parts) <= 1 or (len(parts) == 2 and not parts[1]):
        return "accueil"

    slug = "-".join(p for p in parts[1:] if p)
    slug = re.sub(r"[?#&=].*", "", slug)
    slug = re.sub(r"[^a-z0-9\-]", "-", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")

    # Simplifier les préfixes redondants du site archi
    # /archi/archi-works/xxx → archi-works-xxx (pas archi-archi-works-xxx)
    slug = re.sub(r"^archi-archi-works-", "archi-works-", slug)
    slug = re.sub(r"^archi-archi-news-", "archi-news-", slug)
    slug = re.sub(r"^archi-archi-", "archi-", slug)

    return slug or "page"


def _detect_site_label(url: str) -> str:
    if "archi" in url.lower():
        return "archi"
    return "studio"


def _extract_metadata(page_data: dict) -> dict:
    """Extrait les métadonnées utiles d'une page Firecrawl."""
    metadata = page_data.get("metadata", {})
    markdown  = page_data.get("markdown", "")

    # H1 : première ligne # du markdown
    h1 = ""
    h2_list = []
    ctas = []
    internal_links = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not h1:
            h1 = stripped[2:]
        elif stripped.startswith("## "):
            h2_list.append(stripped[3:])
        # CTAs : lignes avec [texte](url) contenant des mots d'action
        elif re.search(r"\[(nous contacter|contact|devis|soumission|parler|réserver|book|commencer|voir|découvrir)", stripped, re.I):
            links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", stripped)
            ctas.extend(links)

    # Liens internes : liens relatifs ou vers le même domaine
    all_links = re.findall(r"\[([^\]]+)\]\((/[^)]*|https?://(?:awelldone\.studio|welldone\.archi)[^)]*)\)", markdown)
    internal_links = [{"text": t, "url": u} for t, u in all_links[:15]]

    return {
        "h1": h1 or metadata.get("title", ""),
        "meta_description": metadata.get("description", ""),
        "title_tag": metadata.get("title", ""),
        "h2_list": h2_list[:8],
        "ctas": [{"text": t, "url": u} for t, u in ctas[:5]],
        "internal_links": internal_links,
        "status_code": metadata.get("statusCode", 200),
    }


def _clean_markdown(raw_md: str) -> str:
    """
    Nettoie le Markdown Firecrawl :
    - Retire les blocs de navigation répétitifs (header/footer)
    - Retire les lignes vides excessives
    - Retire les éléments purement visuels (lignes de tirets, etc.)
    """
    lines = raw_md.splitlines()
    cleaned = []
    skip_patterns = [
        r"^\[Skip to",
        r"^!\[.*\]\(.*\)$",  # images seules
        r"^---+$",            # séparateurs
        r"^\* \* \*",
        r"^©",
        r"Politique de confidentialité|Privacy Policy",
    ]

    for line in lines:
        skip = any(re.search(p, line.strip(), re.I) for p in skip_patterns)
        if not skip:
            cleaned.append(line)

    # Retirer les blocs de lignes vides consécutives (max 2)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned))
    return result.strip()


def _build_md_file(url: str, markdown: str, metadata: dict, crawl_date: str, site: str) -> str:
    """Construit le contenu final du fichier .md."""
    slug = _url_to_filename(url)
    page_type = _classify_url(url)

    frontmatter = (
        f"---\n"
        f"url: {url}\n"
        f"type: {page_type}\n"
        f"site: {site}\n"
        f"slug: {slug}\n"
        f"crawl_date: {crawl_date}\n"
        f"---\n\n"
    )

    meta_section = "\n\n---\n**Métadonnées crawl**\n"
    if metadata.get("h1"):
        meta_section += f"- **H1 :** {metadata['h1']}\n"
    if metadata.get("meta_description"):
        meta_section += f"- **Meta description :** {metadata['meta_description']}\n"
    if metadata.get("h2_list"):
        meta_section += f"- **H2 :** {' · '.join(metadata['h2_list'][:4])}\n"
    if metadata.get("internal_links"):
        links_str = ", ".join(f"[{l['text']}]({l['url']})" for l in metadata["internal_links"][:5])
        meta_section += f"- **Liens internes :** {links_str}\n"
    if metadata.get("ctas"):
        cta_str = " · ".join(t for t, _ in [(c["text"], c["url"]) for c in metadata["ctas"]])
        meta_section += f"- **CTAs :** {cta_str}\n"

    return frontmatter + markdown + meta_section


class SiteCrawlerAgent(BaseAgent):
    """
    Agent de crawl de site web vers Markdown Obsidian.
    Produit une version textuelle complète du site, sans images,
    organisée par type de page dans 05-Marketing/Site-web/.
    """

    name        = "site"
    description = "Crawler site web → Markdown Obsidian éditable (texte, sans images)"
    schedules: list = []

    # Stockage en mémoire du dernier rapport (pour /site rapport)
    _last_report: dict = {}

    @property
    def commands(self) -> dict:
        return {
            "crawl":   self.crawl,
            "page":    self.scrape_page,
            "rapport": self.rapport,
        }

    # ── Commande principale : crawl ────────────────────────────────────────────

    async def crawl(self, context: dict | None = None) -> str:
        """
        Crawl complet d'un site.
        context["args"] peut contenir "archi" pour crawl welldone.archi.
        """
        ctx = context or {}
        # cmd_agent met le premier arg positionnel dans ctx["id"]
        # Ex: /site crawl archi → ctx["id"] = "archi"
        args = str(ctx.get("args", ctx.get("id", ctx.get("url", "")))).lower()

        if "archi" in args:
            domain = SITE_ARCHI_URL
            site   = "archi"
            label  = "welldone.archi"
        else:
            domain = SITE_STUDIO_URL
            site   = "studio"
            label  = "awelldone.studio"

        if not FIRECRAWL_API_KEY:
            return "❌ FIRECRAWL_API_KEY manquante. Ajouter dans Railway ou .env local."

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return "❌ firecrawl-py non installé. Lancer : pip install firecrawl-py"

        out_dir = self._get_output_dir(site)
        crawl_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        log.info(f"[site] Démarrage crawl {label} → {out_dir}")

        try:
            fc_app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)

            # firecrawl-py v2 : crawl() au lieu de crawl_url()
            # Synchrone + poll → run_in_executor pour ne pas bloquer Telegram
            loop = asyncio.get_event_loop()

            def _do_crawl():
                # firecrawl-py v2 — API confirmée par introspection
                # scrape_options = firecrawl.v2.types.ScrapeOptions
                from firecrawl.v2.types import ScrapeOptions as _SO
                scrape_opts = _SO(
                    formats=["markdown"],
                    only_main_content=True,
                    exclude_tags=["img", "svg", "nav", "header", "footer", "script", "style"],
                )
                return fc_app.crawl(
                    domain,
                    limit=100,
                    scrape_options=scrape_opts,
                    exclude_paths=["/cdn-cgi/", "/api/", "/_next/", "/static/", "/tag/"],
                    poll_interval=5,
                )

            response = await loop.run_in_executor(None, _do_crawl)
        except Exception as e:
            log.error(f"[site] Erreur Firecrawl : {e}")
            return f"❌ Erreur Firecrawl : {e}"

        # Traitement des pages — firecrawl-py v2 retourne un CrawlJob
        # CrawlJob est itérable et yield des Document (attributs .markdown, .metadata, .url)
        pages = []
        raw_pages = []
        if isinstance(response, dict):
            raw_pages = response.get("data", [])
        elif hasattr(response, "data"):
            raw_pages = list(response.data or [])
        else:
            try:
                raw_pages = list(response)
            except Exception:
                raw_pages = []

        for item in raw_pages:
            if isinstance(item, dict):
                pages.append(item)
            else:
                # Document v2 → convertir en dict compatible
                md = getattr(item, "markdown", "") or ""
                meta = getattr(item, "metadata", None) or {}
                if not isinstance(meta, dict):
                    meta = {
                        k: getattr(meta, k, "")
                        for k in ["url", "title", "description", "statusCode"]
                        if hasattr(meta, k)
                    }
                item_url = getattr(item, "url", "") or meta.get("url", "")
                pages.append({"markdown": md, "metadata": meta, "url": item_url})

        stats = {
            "total_found": len(pages),
            "written": 0,
            "errors": 0,
            "by_type": {"pages": 0, "services": 0, "projets": 0, "articles": 0},
            "files": [],
        }

        for page_data in pages:
            try:
                url      = page_data.get("metadata", {}).get("url", "") or page_data.get("url", "")
                markdown = page_data.get("markdown", "")

                if not url or not markdown or len(markdown.strip()) < 50:
                    continue

                metadata = _extract_metadata(page_data)
                cleaned  = _clean_markdown(markdown)
                content  = _build_md_file(url, cleaned, metadata, crawl_date, site)

                page_type = _classify_url(url)
                filename  = _url_to_filename(url) + ".md"
                filepath  = out_dir / page_type / filename

                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(content, encoding="utf-8")

                stats["written"] += 1
                stats["by_type"][page_type] = stats["by_type"].get(page_type, 0) + 1
                stats["files"].append({"url": url, "type": page_type, "file": str(filepath.name)})
                log.info(f"[site] ✓ {page_type}/{filename}")

            except Exception as e:
                log.error(f"[site] Erreur page {page_data.get('url', '?')} : {e}")
                stats["errors"] += 1

        # Générer les fichiers index
        self._write_index(out_dir, stats, label, crawl_date, domain)
        self._write_report(out_dir, stats, label, crawl_date)

        # Mémoriser pour /site rapport
        SiteCrawlerAgent._last_report = {
            "label": label, "date": crawl_date, **stats
        }

        # Résumé Telegram
        vault_note = ""
        if OBSIDIAN_VAULT_PATH and Path(OBSIDIAN_VAULT_PATH).exists():
            vault_note = "\n📂 Fichiers dans Obsidian `05-Marketing/Site-web/`"
        else:
            vault_note = f"\n📁 Fichiers dans `/tmp/site-mirror/{site}/`"

        return (
            f"✅ *Crawl {label} terminé*\n\n"
            f"📄 Pages trouvées : {stats['total_found']}\n"
            f"✍️ Fichiers écrits : {stats['written']}\n"
            f"❌ Erreurs : {stats['errors']}\n\n"
            f"📊 Par type :\n"
            f"  • pages : {stats['by_type'].get('pages', 0)}\n"
            f"  • services : {stats['by_type'].get('services', 0)}\n"
            f"  • projets : {stats['by_type'].get('projets', 0)}\n"
            f"  • articles : {stats['by_type'].get('articles', 0)}\n"
            f"{vault_note}"
        )

    # ── Commande : scrape une page précise ────────────────────────────────────

    async def scrape_page(self, context: dict | None = None) -> str:
        """Scrape une URL précise et l'écrit dans Obsidian."""
        ctx = context or {}
        # Accepte --url https://..., ctx["id"] (arg positionnel), ou ctx["args"]
        url = ctx.get("url", ctx.get("args", ctx.get("id", "")))

        if not url:
            return "❌ URL manquante. Usage : /site page --url https://awelldone.studio/a-propos"

        if not FIRECRAWL_API_KEY:
            return "❌ FIRECRAWL_API_KEY manquante."

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return "❌ firecrawl-py non installé."

        site      = _detect_site_label(url)
        out_dir   = self._get_output_dir(site)
        crawl_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        try:
            fc_app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
            loop   = asyncio.get_event_loop()

            def _do_scrape():
                # firecrawl-py v2 — scrape() avec kwargs directs (pas de ScrapeOptions)
                return fc_app.scrape(
                    url,
                    formats=["markdown"],
                    only_main_content=True,
                    exclude_tags=["img", "svg", "nav", "header", "footer", "script", "style"],
                )

            page = await loop.run_in_executor(None, _do_scrape)
        except Exception as e:
            return f"❌ Erreur Firecrawl : {e}"

        # v2 : Document avec attributs directs ; v1 : dict
        if isinstance(page, dict):
            markdown = page.get("markdown", "")
            page_dict = page
        else:
            markdown = getattr(page, "markdown", "") or ""
            meta = getattr(page, "metadata", None) or {}
            if not isinstance(meta, dict):
                meta = {k: getattr(meta, k, "") for k in ["url", "title", "description"] if hasattr(meta, k)}
            page_dict = {"markdown": markdown, "metadata": meta}
        if not markdown:
            return f"❌ Aucun contenu extrait de {url}"

        metadata  = _extract_metadata(page_dict)
        cleaned   = _clean_markdown(markdown)
        content   = _build_md_file(url, cleaned, metadata, crawl_date, site)

        page_type = _classify_url(url)
        filename  = _url_to_filename(url) + ".md"
        filepath  = out_dir / page_type / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")

        return (
            f"✅ *Page scrapée et enregistrée*\n\n"
            f"🔗 URL : {url}\n"
            f"📁 Type : {page_type}\n"
            f"📄 Fichier : `{page_type}/{filename}`\n"
            f"📝 H1 : {metadata.get('h1', 'non détecté')}\n"
            f"📏 Contenu : {len(cleaned)} caractères"
        )

    # ── Commande : rapport du dernier crawl ───────────────────────────────────

    async def rapport(self, context: dict | None = None) -> str:
        """Affiche les stats du dernier crawl."""
        r = SiteCrawlerAgent._last_report

        # Essayer de lire depuis le fichier si pas en mémoire
        if not r:
            for site in ("studio", "archi"):
                report_path = self._get_output_dir(site) / "_crawl-report.md"
                if report_path.exists():
                    return f"📊 *Dernier rapport ({site}) :*\n\n" + report_path.read_text(encoding="utf-8")[:800]
            return "ℹ️ Aucun crawl effectué dans cette session. Lance `/site crawl` d'abord."

        return (
            f"📊 *Dernier crawl — {r.get('label', '?')}*\n"
            f"🕐 Date : {r.get('date', '?')}\n\n"
            f"📄 Pages trouvées : {r.get('total_found', 0)}\n"
            f"✍️ Fichiers écrits : {r.get('written', 0)}\n"
            f"❌ Erreurs : {r.get('errors', 0)}\n\n"
            f"📊 Par type :\n"
            f"  • pages : {r.get('by_type', {}).get('pages', 0)}\n"
            f"  • services : {r.get('by_type', {}).get('services', 0)}\n"
            f"  • projets : {r.get('by_type', {}).get('projets', 0)}\n"
            f"  • articles : {r.get('by_type', {}).get('articles', 0)}"
        )

    # ── Helpers internes ──────────────────────────────────────────────────────

    def _get_output_dir(self, site: str) -> Path:
        """Retourne le répertoire de sortie selon l'environnement."""
        base = Path(OBSIDIAN_VAULT_PATH) if OBSIDIAN_VAULT_PATH else None

        if base and base.exists():
            return base / "05-Marketing" / "Site-web"
        else:
            # Fallback Railway : /tmp/site-mirror/
            return _TMP_DIR / site

    def _write_index(self, out_dir: Path, stats: dict, label: str, date: str, domain: str):
        """Génère _index.md avec toutes les pages trouvées."""
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Index Site — {label}",
            f"**Dernier crawl :** {date}",
            f"**Domaine :** {domain}",
            f"**Pages écrites :** {stats['written']} / {stats['total_found']}",
            "",
            "---",
            "",
            "## Pages trouvées",
            "",
            "| Type | Fichier | URL |",
            "|------|---------|-----|",
        ]

        for f in stats["files"]:
            lines.append(f"| {f['type']} | `{f['file']}` | {f['url']} |")

        lines += [
            "",
            "---",
            "",
            "## Navigation",
            "",
            "- [[05-Marketing/Site-web/pages/]] — Pages générales",
            "- [[05-Marketing/Site-web/services/]] — Pages services",
            "- [[05-Marketing/Site-web/projets/]] — Pages portfolio",
            "- [[05-Marketing/Site-web/articles/]] — Articles blog",
            "- [[05-Marketing/Site-web/analyse/]] — Analyses éditoriales",
        ]

        (out_dir / "_index.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_report(self, out_dir: Path, stats: dict, label: str, date: str):
        """Génère _crawl-report.md avec les statistiques."""
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Rapport de Crawl — {label}",
            f"**Date :** {date}",
            "",
            "## Statistiques",
            "",
            f"| Métrique | Valeur |",
            f"|----------|--------|",
            f"| Pages trouvées | {stats['total_found']} |",
            f"| Fichiers écrits | {stats['written']} |",
            f"| Erreurs | {stats['errors']} |",
            f"| Pages | {stats['by_type'].get('pages', 0)} |",
            f"| Services | {stats['by_type'].get('services', 0)} |",
            f"| Projets | {stats['by_type'].get('projets', 0)} |",
            f"| Articles | {stats['by_type'].get('articles', 0)} |",
            "",
            "## Actions recommandées",
            "",
            "- [ ] Vérifier la classification des pages dans `_index.md`",
            "- [ ] Ouvrir `analyse/_audit-editorial.md` pour l'audit éditorial",
            "- [ ] Identifier les pages à réécrire en priorité",
        ]

        (out_dir / "_crawl-report.md").write_text("\n".join(lines), encoding="utf-8")


# ── Exposition de l'agent (requis par le dispatcher) ─────────────────────────
agent = SiteCrawlerAgent()
