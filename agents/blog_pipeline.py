"""
agents/blog_pipeline.py — Pipeline blog 3 étapes (fire-and-forget).

Architecture :
  /blog rédiger <sujet>
    ↓ Dispatcher CEO (Python pur, aucun appel Claude ici)
    ↓ étape 1 — framer.rédiger   (texte, 1 appel Claude max)
    ↓ étape 2 — framer.illustrer (images Gemini, 0 appel Claude)
    ↓ étape 3 — qualite.vérifier (1 appel Claude, score JSON)
    ↓ Notification Telegram avec lien + score

Guardrails ABSOLUS :
  - Pipeline linéaire : aucun agent ne rappelle un autre
  - 15 000 tokens max pour tout le pipeline
  - Timeouts durs : rédaction 200s, images 240s, qualité 30s, total 360s
  - Si qualité < 6/10 → notifier JP mais NE PAS régénérer
  - Chaque étape échoue gracieusement (continue à l'étape suivante)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from agents._base import BaseAgent
from core.guardrails import SessionBudget, BudgetExceededError

# ── Registre de publication ────────────────────────────────────────────────────
# Permet aux boutons Telegram inline de retrouver le slug à publier.
# Clé courte (8 hex) → slug complet.  Callback data: "pub_{key}" ≤ 12 chars.
_pub_registry: dict[str, str] = {}


def _register_pub_slug(slug: str) -> str:
    """Enregistre le slug et retourne la clé courte pour le callback."""
    key = hashlib.md5(slug.encode()).hexdigest()[:8]
    _pub_registry[key] = slug
    return key

log = logging.getLogger(__name__)


# ── Pipeline Budget ────────────────────────────────────────────────────────────

class PipelineBudgetError(Exception):
    """Levée quand le budget token ou temps du pipeline est dépassé."""


class PipelineBudget:
    """
    Compteur token + chrono pour l'ensemble du pipeline.
    Budget dur : 15 000 tokens, 600s.
    """
    max_tokens: int = 15_000
    max_seconds: int = 480  # 8 min — rédaction ~90s + images+publish ~240s + qualité ~30s + overhead

    def __init__(self):
        self.used_tokens: int = 0
        self.start_time: float | None = None
        # SessionBudget délégué (pour safe_claude_call)
        self._session = SessionBudget(limit=self.max_tokens)

    @property
    def session(self) -> SessionBudget:
        return self._session

    def start(self) -> None:
        self.start_time = time.time()

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    def sync_tokens(self) -> None:
        """Synchronise used_tokens depuis le SessionBudget interne."""
        self.used_tokens = self._session.total

    def check(self) -> None:
        """Lève PipelineBudgetError si tokens ou temps dépassés."""
        self.sync_tokens()
        if self.used_tokens >= self.max_tokens:
            raise PipelineBudgetError(
                f"Budget pipeline épuisé : {self.used_tokens}/{self.max_tokens} tokens."
            )
        elapsed = self.elapsed()
        if elapsed >= self.max_seconds:
            raise PipelineBudgetError(
                f"Timeout pipeline dépassé : {elapsed:.0f}s/{self.max_seconds}s."
            )


# ── Agent pipeline ─────────────────────────────────────────────────────────────

class BlogPipelineAgent(BaseAgent):
    name = "blog"
    description = "Pipeline blog complet : rédaction + images + qualité (fire-and-forget)"
    schedules: list = []

    @property
    def commands(self):
        return {"rédiger": self.rediger}

    async def rediger(self, context: dict | None = None) -> str:
        """
        Commande principale. Lance le pipeline en background et retourne immédiatement.

        context: {"sujet": str — le sujet de l'article}
        """
        ctx = context or {}
        sujet = ctx.get("sujet", "").strip()
        if not sujet:
            return "❌ `blog rédiger` nécessite un sujet. Ex: `/blog rédiger la valeur de la photo pro`"

        log.info(f"blog_pipeline: lancement pipeline — sujet={sujet[:80]!r}")

        # Fire-and-forget : la tâche tourne en arrière-plan
        asyncio.create_task(self._run_pipeline(sujet))

        return (
            "🚀 *Pipeline lancé* — je te notifie quand c'est prêt.\n"
            f"_Sujet : {sujet[:100]}_\n\n"
            "Étapes : ✍️ Rédaction → 🖼 Images → ✅ Qualité"
        )

    async def _run_pipeline(self, sujet: str) -> None:
        """
        Orchestre le pipeline linéaire : rédiger → illustrer → qualité → notification.

        Pas de retry automatique — si l'étape 1 échoue, on notifie JP directement.
        Le score qualité est informatif uniquement, l'article n'est jamais supprimé.
        """
        from core.telegram_notifier import notify
        from core.dispatcher import dispatch

        budget = PipelineBudget()
        budget.start()

        article_result: dict = {}
        images_result: dict = {}
        qualite_result: dict = {}
        _cached_article: dict = {}
        etape1_ok = False
        images_ok = False
        img_count = 0
        actual_slug = ""

        # ── Étape 1 : rédaction ────────────────────────────────────────────────
        log.info(f"blog_pipeline: étape 1 — framer.rédiger — sujet={sujet[:60]!r}")
        try:
            budget.check()
            ctx_rediger: dict = {
                "sujet": sujet,
                "_pipeline_budget": budget.session,
            }
            raw1 = await asyncio.wait_for(
                dispatch("framer", "rédiger", ctx_rediger),
                timeout=200,  # rédiger + QA verify peut prendre jusqu'à 180s
            )
            article_result = {"raw": raw1, "sujet": sujet}
            etape1_ok = True
            actual_slug = _extract_slug(raw1)
            if actual_slug:
                log.info(f"blog_pipeline: slug extrait = {actual_slug!r}")
            else:
                log.warning("blog_pipeline: slug non trouvé dans la réponse rédiger")

            # Lire le contenu réel depuis le cache MAINTENANT (avant qu'illustrer ne le supprime)
            from agents.framer import _article_cache
            _cache_keys = list(_article_cache.keys())
            log.info(f"blog_pipeline: cache keys={_cache_keys[:5]} actual_slug={actual_slug!r}")
            _cached_qa = _article_cache.get(actual_slug, {}) if actual_slug else {}
            _cached_article = _cached_qa.get("article", {})
            if not _cached_article and _cache_keys:
                # Fallback : chercher par correspondance partielle (slug peut différer)
                for _ck in reversed(_cache_keys):
                    if actual_slug and (actual_slug in _ck or _ck in actual_slug):
                        _cached_qa = _article_cache[_ck]
                        _cached_article = _cached_qa.get("article", {})
                        log.info(f"blog_pipeline: cache hit partiel — clé={_ck!r}")
                        break
                if not _cached_article and _cache_keys:
                    # Dernier recours : le dernier item du cache
                    _last_key = _cache_keys[-1]
                    _cached_qa = _article_cache[_last_key]
                    _cached_article = _cached_qa.get("article", {})
                    log.info(f"blog_pipeline: cache fallback dernier item — clé={_last_key!r}")
            log.info(f"blog_pipeline: _cached_article keys={list(_cached_article.keys())[:8] if _cached_article else '(vide)'}")

            budget.sync_tokens()
            log.info(f"blog_pipeline: étape 1 OK — tokens={budget.used_tokens}")
            await notify(
                f"✍️ *Rédaction OK* — génération des images Gemini en cours…\n"
                f"_Sujet : {sujet[:80]}_"
            )

        except asyncio.TimeoutError:
            log.error("blog_pipeline: étape 1 TIMEOUT (200s)")
            await notify(
                f"⛔ *Pipeline blog — timeout rédaction (200s)*\n\n"
                f"📝 _{sujet[:100]}_\n"
                f"_Durée : {budget.elapsed():.0f}s_\n\n"
                f"💡 Réessaie avec `/blog rédiger {sujet[:60]}`"
            )
            return
        except PipelineBudgetError as e:
            log.error(f"blog_pipeline: BUDGET ÉPUISÉ étape 1: {e}")
            await notify(
                f"⛔ *Pipeline blog — budget épuisé*\n\n"
                f"📝 _{sujet[:100]}_\n"
                f"_{e}_\n"
                f"_Durée : {budget.elapsed():.0f}s_"
            )
            return
        except Exception as e:
            log.error(f"blog_pipeline: étape 1 ERREUR: {e}", exc_info=True)
            await notify(
                f"⛔ *Pipeline blog — erreur rédaction*\n\n"
                f"📝 _{sujet[:100]}_\n"
                f"_{str(e)[:200]}_"
            )
            return

        # ── Étape 2 : images ──────────────────────────────────────────────────
        log.info("blog_pipeline: étape 2 — framer.illustrer")
        try:
            budget.check()
            ctx_illustrer: dict = {"sujet": sujet}
            if actual_slug:
                ctx_illustrer["slug"] = actual_slug
            raw2 = await asyncio.wait_for(
                dispatch("framer", "illustrer", ctx_illustrer),
                timeout=300,  # images ~60s + delete/recreate ~30s + publish ~60s + sleep 15s + marge
            )
            images_result = {"raw": raw2}
            images_ok = True
            import re as _re
            _m = _re.search(r'\b([1-9][0-9]?)\s*image', raw2, flags=_re.IGNORECASE)
            img_count = int(_m.group(1)) if _m else (0 if "❌" in raw2 or "erreur" in raw2.lower() else 1)
            log.info(f"blog_pipeline: étape 2 OK — img_count={img_count}")
        except asyncio.TimeoutError:
            log.warning("blog_pipeline: étape 2 TIMEOUT — continue sans images")
            images_result = {"erreur": "timeout images 150s"}
        except PipelineBudgetError as e:
            log.warning(f"blog_pipeline: budget à l'étape images — continue quand même: {e}")
            images_result = {"erreur": str(e)}
        except Exception as e:
            log.warning(f"blog_pipeline: étape 2 ERREUR (continue): {e}")
            images_result = {"erreur": str(e)[:200]}

        # ── Étape 3 : qualité (informative seulement) ─────────────────────────
        log.info("blog_pipeline: étape 3 — qualite.vérifier")
        try:
            budget.check()

            # Utiliser le contenu réel capturé avant illustrer (avant le pop du cache)
            titre = _cached_article.get("Title", "") or _extract_titre(article_result.get("raw", ""), sujet)

            if _cached_article:
                # Assembler un extrait depuis les vrais champs de l'article Claude
                parts = []
                for field in ("Heading1-Text", "Heading1-Titre", "Heading2-Text",
                              "Heading2-Titre", "Heading3-Text", "Sous-Titre (gauche)"):
                    val = _cached_article.get(field, "")
                    if val and len(val) > 20:
                        parts.append(val[:250])
                contenu_sample = " ".join(parts)[:800] or f"Article sur : {sujet}"
            else:
                contenu_sample = f"Article sur : {sujet}"

            from agents.qualite import agent as qualite_agent
            qualite_result = await asyncio.wait_for(
                qualite_agent.verifier_article(
                    {
                        "titre": titre,
                        "contenu_sample": contenu_sample,
                        "sujet": sujet,
                        "img_count": img_count,
                    },
                    budget=budget.session,
                ),
                timeout=30,
            )
            budget.sync_tokens()
            score = qualite_result.get("score", 0)
            log.info(f"blog_pipeline: étape 3 OK — score={score}/10")

        except asyncio.TimeoutError:
            qualite_result = {"score": 0, "ok": True, "raison": "Scoring timeout (30s) — article publié"}
        except Exception as e:
            log.error(f"blog_pipeline: étape 3 ERREUR: {e}", exc_info=True)
            qualite_result = {"score": 0, "ok": True, "raison": f"Scoring indisponible"}

        # ── Notification finale ────────────────────────────────────────────────
        budget.sync_tokens()  # BUG 12 fix : sync tokens avant notification
        log.info(f"blog_pipeline: pipeline terminé — tokens={budget.used_tokens} notification JP")
        await self._notify_done(
            sujet=sujet,
            slug=actual_slug,
            etape1_ok=etape1_ok,
            images_ok=images_ok,
            article_result=article_result,
            images_result=images_result,
            qualite_result=qualite_result,
            budget=budget,
            attempt=1,
            cached_article=_cached_article,
        )

    async def _notify_done(
        self,
        sujet: str,
        slug: str = "",
        etape1_ok: bool = True,
        images_ok: bool = False,
        article_result: dict = None,
        images_result: dict = None,
        qualite_result: dict = None,
        budget: PipelineBudget = None,
        attempt: int = 1,
        cached_article: dict = None,
    ) -> None:
        """Envoie la notification de succès à JP via Telegram + crée page Notion."""
        from core.telegram_notifier import notify
        article_result  = article_result  or {}
        images_result   = images_result   or {}
        qualite_result  = qualite_result  or {}
        cached_article  = cached_article  or {}

        score = qualite_result.get("score", 0)
        raison = qualite_result.get("raison", "")
        elapsed = budget.elapsed() if budget else 0
        tokens_used = budget.used_tokens if budget else 0
        max_tokens = budget.max_tokens if budget else 15_000

        score_emoji = "🟢" if score >= 8 else "🟡"
        retry_note = f" _(corrigé en {attempt} tentative{'s' if attempt > 1 else ''})_" if attempt > 1 else ""

        # URL staging (framer.app) — retournée par illustrer()
        import re as _re_stage
        staging_lien = ""
        img_raw = images_result.get("raw", "")
        # Extraire le flag staging_url_verified encodé dans le retour de illustrer
        _sv_match = _re_stage.search(r"_staging_url_verified:(True|False)_", img_raw)
        staging_url_verified = (_sv_match.group(1) == "True") if _sv_match else False
        if img_raw:
            staging_lien = _extract_lien(img_raw)
        if not staging_lien and slug:
            from agents.framer import FRAMER_STAGING_URL
            if FRAMER_STAGING_URL:
                staging_lien = f"{FRAMER_STAGING_URL.rstrip('/')}/journal/{slug}"
        if not staging_lien:
            from agents.framer import FRAMER_PROJECT_ID
            staging_lien = f"https://framer.com/projects/Welldone-Studio--{FRAMER_PROJECT_ID}"

        # ── Créer page Notion avec contenu complet (BUG 11 fix) ───────────────
        notion_url = ""
        try:
            from core.notion_delivery import pipeline_create
            titre = cached_article.get("Title", "") or _extract_titre(article_result.get("raw", ""), sujet)
            # Assembler le contenu de l'article pour Notion
            _parts = [f"# {titre}\n"]
            for _h in ("Heading1-Titre", "Heading1-Text", "Heading2-Titre", "Heading2-Text",
                        "Heading3-Titre", "Heading3-Text", "Heading4-Titre", "Heading4-Text",
                        "Heading5-Titre", "Heading5-Text"):
                val = cached_article.get(_h, "")
                if val:
                    if _h.endswith("-Titre"):
                        _parts.append(f"\n## {val}\n")
                    else:
                        _parts.append(val)
            _content = "\n".join(_parts) if len(_parts) > 1 else f"Article sur : {sujet}"
            notion_url = await pipeline_create(
                title=titre or sujet[:100],
                agent="blog",
                type_="article",
                content=_content,
                framer_url=staging_lien,
                status="Prêt révision",
            ) or ""
        except Exception as _ne:
            log.warning(f"blog_pipeline: Notion pipeline_create skip ({_ne})")

        if staging_url_verified:
            staging_label = f"👁 [Réviser en staging]({staging_lien})"
        else:
            staging_label = f"👁 [Staging]({staging_lien}) _(accessible dans ~2 min)_"

        lines = [
            f"✅ *Article créé — prêt à réviser !*{retry_note}",
            f"📝 _{sujet[:100]}_",
            "",
            f"{score_emoji} Qualité : *{score}/10* — _{raison[:120]}_",
            f"{'✅' if images_ok else '⚠️'} Images",
            "",
            staging_label,
        ]
        if notion_url:
            lines.append(f"📋 [Voir dans Notion]({notion_url})")
        lines.append(f"\n_Durée : {elapsed:.0f}s | Tokens : {tokens_used}/{max_tokens}_")

        msg = "\n".join(lines)

        # Bouton inline "Publier sur awelldone.com" si on a le slug
        keyboard = None
        if slug:
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                pub_key = _register_pub_slug(slug)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🌐 Publier le site",
                        callback_data=f"pub_{pub_key}",
                    )
                ]])
            except Exception as _ke:
                log.warning(f"blog_pipeline: keyboard build failed: {_ke}")

        log.info(f"blog_pipeline: succès notifié — score={score}/10 tokens={tokens_used}")
        await notify(msg, reply_markup=keyboard)


# ── Utilitaires ────────────────────────────────────────────────────────────────

def _extract_titre(text: str, fallback: str) -> str:
    """
    Tente d'extraire un titre de la réponse framer.rédiger.
    Cherche des patterns courants : "# Titre", "**Titre**", première ligne non vide.
    """
    if not text:
        return fallback[:100]

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Markdown h1
        if line.startswith("# "):
            return line[2:].strip()[:120]
        # Markdown bold
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            return line[2:-2].strip()[:120]
        # Première ligne non vide (heuristique)
        if len(line) > 10:
            return line[:120]

    return fallback[:100]


def _extract_lien(text: str) -> str:
    """Extrait le premier lien https:// trouvé dans le texte."""
    if not text:
        return ""
    import re
    match = re.search(r"https?://[^\s\)>\]\"']+", text)
    return match.group(0).rstrip(".,;") if match else ""


def _extract_slug(text: str) -> str:
    """
    Extrait le slug d'article depuis la réponse de framer.rédiger.
    Cherche les patterns : /journal/<slug>, Deployment: `xxx`, staging URL.
    """
    if not text:
        return ""
    import re
    # Pattern: /journal/some-slug
    match = re.search(r"/journal/([\w\-]+)", text)
    if match:
        return match.group(1)
    # Pattern: slug=some-slug
    match = re.search(r"slug[=:][\s`'\"]*([a-z0-9\-]{10,})", text)
    if match:
        return match.group(1)
    return ""


agent = BlogPipelineAgent()
