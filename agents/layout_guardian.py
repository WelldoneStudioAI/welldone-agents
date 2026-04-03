"""
agents/layout_guardian.py — Framer Layout Guardian

Rôle : détecter les problèmes de mise en page sur awelldone.studio
       et proposer des corrections minimales. Ne redesigne JAMAIS.

Règle centrale : "Preserve intent, fix inconsistency."

Commandes :
  inspecter  → audit complet d'une page (URL ou slug)
  juge       → vérifie qu'une correction proposée est minimale et sûre
  rapport    → récupère le dernier rapport stocké
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from agents._base import BaseAgent
from core.brain import get_client
from core.guardrails import safe_claude_call, SessionBudget
from config import CLAUDE_MODEL, FRAMER_STAGING_URL

log = logging.getLogger(__name__)

_STAGING_BASE = FRAMER_STAGING_URL or "https://fabulous-selfies-613710.framer.app"
_PROD_BASE    = "https://awelldone.studio"

# ── Stockage en mémoire du dernier rapport (par URL) ─────────────────────────
_last_reports: dict[str, dict] = {}

# ── Prompt Inspection ─────────────────────────────────────────────────────────
_INSPECT_SYSTEM = """\
Tu es le Framer Layout Guardian de Welldone Studio.

TON RÔLE : détecter les problèmes de mise en page et proposer des corrections MINIMALES.
Tu ne redesignes JAMAIS. Tu ne changes pas l'intention visuelle. Tu corriges les écarts.

RÈGLE CENTRALE : "Preserve intent, fix inconsistency."

GUARDRAILS ABSOLUS :
1. No redesign — interdit de modifier l'esthétique globale
2. Minimal change only — chaque correction = la plus petite modification possible
3. Evidence before change — chaque correction justifiée par un problème visible (overflow, wrap cassé, collision, break mobile, désalignement mesurable)
4. Homogeneity over novelty — aligner vers le pattern dominant déjà présent
5. No self-approval — tu ne valides jamais toi-même "c'est bon"

CE QUE TU DÉTECTES :
- Texte tronqué dans card, thumbnail, badge, bouton, champ
- Texte trop grand qui casse une ligne ou sort du conteneur
- Overflow horizontal ou vertical
- Espacement incohérent entre sections, cartes, titres, CTA
- Alignements irréguliers
- Colonnes ou grilles qui se cassent mal sur mobile
- Responsive tablette/mobile dégradé
- Composants "presque pareils" mais pas homogènes
- Hiérarchie typo incohérente
- Padding/margin non conformes au pattern dominant

FORMAT DE SORTIE — UNIQUEMENT ce JSON :
{
  "page": "URL ou slug",
  "issues": [
    {
      "location": "section/composant précis",
      "issue": "description du problème visible",
      "severity": "low|medium|high",
      "impact": "lisibilité|responsive|cohérence|overflow",
      "minimal_fix": "correction la plus petite possible",
      "why_this_fix": "pourquoi elle respecte le design existant"
    }
  ],
  "summary": "1-2 phrases résumé global",
  "ok_to_publish": true|false,
  "judge_checklist": [
    "Le problème était réel",
    "La correction est minimale",
    "Le layout d'origine est respecté",
    "Le responsive est meilleur sur mobile",
    "Aucune régression visuelle introduite"
  ]
}

Si aucun problème détecté : issues = [], ok_to_publish = true.
Retourne UNIQUEMENT le JSON, sans markdown.\
"""

_JUDGE_SYSTEM = """\
Tu es le Judge / Auditor du Framer Layout Guardian de Welldone Studio.

TON RÔLE : vérifier qu'une correction proposée est minimale, sûre et respecte le design existant.

Tu dois vérifier 5 critères :
1. Le problème était réel (overflow, wrap cassé, désalignement mesurable)
2. La correction est minimale (pas de redesign, pas de changement global)
3. Le layout d'origine est respecté (intention visuelle préservée)
4. Le responsive sera meilleur ou équivalent sur mobile
5. Aucune régression visuelle n'est introduite

CRITÈRE CLÉ : "Fixes inconsistency without changing design intent."

FORMAT DE SORTIE — UNIQUEMENT ce JSON :
{
  "approved": true|false,
  "criteria": {
    "problem_was_real": true|false,
    "correction_is_minimal": true|false,
    "layout_preserved": true|false,
    "responsive_improved": true|false,
    "no_regression": true|false
  },
  "verdict": "explication en 1-2 phrases",
  "blocked_reason": "si approved=false, pourquoi"
}

Retourne UNIQUEMENT le JSON, sans markdown.\
"""


async def _fetch_page_html(url: str) -> str:
    """Fetch la page HTML, retourne le contenu textuel nettoyé (structures DOM pertinentes)."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            }
            resp = await client.get(url, headers=headers)
            html = resp.text

        # Extraire les éléments pertinents pour l'analyse layout
        # Garder : class, style, data-framer, textes
        cleaned = _extract_layout_context(html)
        return cleaned[:12000]  # Limite pour Claude
    except Exception as e:
        log.warning(f"layout_guardian: fetch failed for {url}: {e}")
        return f"[Impossible de fetch {url}: {e}]"


def _extract_layout_context(html: str) -> str:
    """Extrait les infos de layout pertinentes du HTML Framer."""
    # Supprimer scripts et styles internes
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)

    # Extraire les classes CSS et attributs data-framer (contiennent les infos de layout)
    framer_attrs = re.findall(r'data-framer[^=]*="[^"]*"', html)
    classes = re.findall(r'class="([^"]{10,})"', html)
    inline_styles = re.findall(r'style="([^"]{15,})"', html)

    # Extraire les textes visibles
    text_content = re.sub(r"<[^>]+>", " ", html)
    text_content = re.sub(r"\s+", " ", text_content).strip()[:3000]

    # Détecter les overflow/overflow-hidden explicites dans les styles inline
    overflow_hints = [s for s in inline_styles if "overflow" in s.lower() or "width" in s.lower() or "height" in s.lower()][:20]

    summary = f"""=== TEXTES VISIBLES ===
{text_content}

=== ATTRIBUTS FRAMER (layout hints) ===
{chr(10).join(framer_attrs[:50])}

=== CLASSES CSS (layout) ===
{chr(10).join(classes[:30])}

=== STYLES INLINE (overflow/sizing) ===
{chr(10).join(overflow_hints)}"""

    return summary


def _resolve_url(target: str) -> tuple[str, str]:
    """Résout un slug ou URL vers (staging_url, display_name)."""
    if target.startswith("http"):
        return target, target
    # Slug → staging
    slug = target.lstrip("/")
    if slug == "" or slug == "home":
        return _STAGING_BASE, "Page d'accueil"
    return f"{_STAGING_BASE}/{slug}", slug


class LayoutGuardianAgent(BaseAgent):
    name        = "layout_guardian"
    description = "Framer Layout Guardian — détecte et corrige les incohérences de mise en page sans redesign"
    schedules: list = []

    @property
    def commands(self):
        return {
            "inspecter": self.inspecter,
            "juge":      self.juge,
            "rapport":   self.rapport,
        }

    async def inspecter(self, context: dict | None = None) -> str:
        """
        Inspecte une page Framer et retourne un rapport structuré de problèmes de layout.

        Context:
          page (str) : URL complète ou slug (ex: "journal/mon-article", "/", "about")
          breakpoints (list[str]) : ["desktop", "tablet", "mobile"] — défaut: tous les 3
        """
        ctx = context or {}
        target = ctx.get("page") or ctx.get("sujet") or ctx.get("url") or "/"
        breakpoints = ctx.get("breakpoints", ["desktop", "tablet", "mobile"])

        url, display = _resolve_url(target)
        log.info(f"layout_guardian.inspecter: {url}")

        # Fetch le contenu de la page
        html_context = await _fetch_page_html(url)

        # Garde-fou : si le fetch a échoué, ne pas envoyer l'erreur à Claude comme si c'était du HTML
        if html_context.startswith("[Impossible de fetch"):
            log.error(f"layout_guardian: fetch échoué — abandon avant appel Claude. Détail: {html_context}")
            return (
                f"❌ *Layout Guardian — page inaccessible*\n"
                f"URL : `{url}`\n"
                f"Cause : {html_context}\n\n"
                f"_Vérifie que l'URL de staging est configurée dans FRAMER_STAGING_URL "
                f"et que la page est publiée._"
            )

        prompt = f"""Inspecte cette page Framer pour des problèmes de layout.

URL : {url}
Breakpoints à vérifier : {', '.join(breakpoints)}

CONTENU ET STRUCTURE DE LA PAGE :
{html_context}

Identifie tous les problèmes de mise en page visibles ou probables d'après la structure HTML/CSS.
Pour chaque problème, fournis le format JSON spécifié.
"""

        try:
            resp = await safe_claude_call(
                get_client(),
                model=CLAUDE_MODEL,
                max_tokens=3000,
                system=_INSPECT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                timeout_s=90,
                agent_name="layout_guardian.inspecter",
            )
            raw = resp.content[0].text.strip()

            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            report = json.loads(raw)
            report["page"] = url
            _last_reports[url] = report

            # Formater pour Telegram
            return _format_report(report, display)

        except json.JSONDecodeError as e:
            log.error(f"layout_guardian: JSON parse error: {e}")
            return f"❌ Erreur parsing rapport: {e}"
        except Exception as e:
            log.error(f"layout_guardian.inspecter error: {e}", exc_info=True)
            return f"❌ Erreur inspection: {e}"

    async def juge(self, context: dict | None = None) -> str:
        """
        Vérifie qu'une correction proposée est minimale et ne compromet pas le design.

        Context:
          location (str) : section/composant concerné
          issue (str)    : problème détecté
          fix (str)      : correction proposée
        """
        ctx = context or {}
        location = ctx.get("location", "inconnu")
        issue    = ctx.get("issue", "")
        fix      = ctx.get("fix") or ctx.get("minimal_fix", "")

        if not issue or not fix:
            return "❌ Fournis `issue` et `fix` dans le context."

        prompt = f"""Évalue cette correction de layout Framer :

Localisation : {location}
Problème détecté : {issue}
Correction proposée : {fix}

Applique les 5 critères de jugement et retourne le JSON de verdict.\
"""

        try:
            resp = await safe_claude_call(
                get_client(),
                model=CLAUDE_MODEL,
                max_tokens=600,
                system=_JUDGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                timeout_s=45,
                agent_name="layout_guardian.juge",
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            verdict = json.loads(raw)
            return _format_verdict(verdict, location, fix)

        except Exception as e:
            log.error(f"layout_guardian.juge error: {e}")
            return f"❌ Erreur juge: {e}"

    async def rapport(self, context: dict | None = None) -> str:
        """Retourne le dernier rapport stocké pour une page."""
        ctx = context or {}
        target = ctx.get("page") or ctx.get("url") or "/"
        url, _ = _resolve_url(target)

        if url in _last_reports:
            return _format_report(_last_reports[url], url)
        if _last_reports:
            last_url = list(_last_reports.keys())[-1]
            return _format_report(_last_reports[last_url], last_url)
        return "📭 Aucun rapport disponible. Lance `/layout_guardian inspecter` d'abord."


def _format_report(report: dict, display: str) -> str:
    issues = report.get("issues", [])
    ok = report.get("ok_to_publish", len(issues) == 0)
    summary = report.get("summary", "")
    url = report.get("page", display)

    severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    lines = [f"🔍 *Framer Layout Guardian — {display}*\n"]
    lines.append(f"🔗 {url}\n")

    if not issues:
        lines.append("✅ Aucun problème détecté. Layout conforme.")
    else:
        high = sum(1 for i in issues if i.get("severity") == "high")
        med  = sum(1 for i in issues if i.get("severity") == "medium")
        low  = sum(1 for i in issues if i.get("severity") == "low")
        lines.append(f"*{len(issues)} problème(s)* — 🔴 {high} high · 🟡 {med} medium · 🟢 {low} low\n")

        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "low")
            icon = severity_icon.get(sev, "⚪")
            lines.append(
                f"{icon} *{i}. {issue.get('location', '?')}*\n"
                f"   Problème : {issue.get('issue', '')}\n"
                f"   Impact : {issue.get('impact', '')}\n"
                f"   Fix minimal : `{issue.get('minimal_fix', '')}`\n"
                f"   Pourquoi : {issue.get('why_this_fix', '')}\n"
            )

    if summary:
        lines.append(f"\n_{summary}_")

    status = "✅ Publiable" if ok else "🚫 Corrections requises avant publication"
    lines.append(f"\n*Statut :* {status}")
    lines.append("\n_Validé par le Judge avant application de toute correction._")

    return "\n".join(lines)


def _format_verdict(verdict: dict, location: str, fix: str) -> str:
    approved = verdict.get("approved", False)
    criteria = verdict.get("criteria", {})
    v_text   = verdict.get("verdict", "")
    blocked  = verdict.get("blocked_reason", "")

    icon = "✅" if approved else "🚫"
    lines = [f"{icon} *Judge — {location}*\n"]
    lines.append(f"Correction évaluée : `{fix[:100]}`\n")

    c_icons = {
        "problem_was_real":      ("Le problème était réel",             criteria.get("problem_was_real", False)),
        "correction_is_minimal": ("La correction est minimale",         criteria.get("correction_is_minimal", False)),
        "layout_preserved":      ("Layout d'origine respecté",          criteria.get("layout_preserved", False)),
        "responsive_improved":   ("Responsive meilleur ou équivalent",  criteria.get("responsive_improved", False)),
        "no_regression":         ("Aucune régression visuelle",         criteria.get("no_regression", False)),
    }
    for _, (label, val) in c_icons.items():
        lines.append(f"{'✓' if val else '✗'} {label}")

    lines.append(f"\n_{v_text}_")
    if not approved and blocked:
        lines.append(f"\n🚫 Bloqué : {blocked}")

    return "\n".join(lines)


agent = LayoutGuardianAgent()
