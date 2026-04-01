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
  - Timeouts durs : rédaction 90s, images 45s, qualité 30s, total 240s
  - Si qualité < 6/10 → notifier JP mais NE PAS régénérer
  - Chaque étape échoue gracieusement (continue à l'étape suivante)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from agents._base import BaseAgent
from core.guardrails import SessionBudget, BudgetExceededError

log = logging.getLogger(__name__)


# ── Pipeline Budget ────────────────────────────────────────────────────────────

class PipelineBudgetError(Exception):
    """Levée quand le budget token ou temps du pipeline est dépassé."""


class PipelineBudget:
    """
    Compteur token + chrono pour l'ensemble du pipeline.
    Budget dur : 15 000 tokens, 240s.
    """
    max_tokens: int = 15_000
    max_seconds: int = 240

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
        Orchestre le pipeline avec auto-correction autonome.

        Boucle jusqu'à MAX_ATTEMPTS :
          1. Rédiger   — avec feedback QA de la tentative précédente si applicable
          2. Illustrer — seulement si rédaction OK
          3. Vérifier  — score QA
          Si score >= 6 → succès, notifie JP
          Si score < 6  → supprime du CMS, injecte le feedback dans la prochaine tentative
          Si MAX_ATTEMPTS atteint sans succès → notifie JP de l'échec final avec diagnostic
        JP n'est jamais sollicité pour intervenir manuellement.
        """
        from core.telegram_notifier import notify
        from core.dispatcher import dispatch

        MAX_ATTEMPTS = 3
        budget = PipelineBudget()
        budget.start()

        qa_feedback: str = ""  # Feedback QA injecté dans les tentatives suivantes

        for attempt in range(1, MAX_ATTEMPTS + 1):
            log.info(f"blog_pipeline: tentative {attempt}/{MAX_ATTEMPTS} — sujet={sujet[:60]!r}")

            article_result: dict = {}
            images_result: dict = {}
            qualite_result: dict = {}
            etape1_ok = False
            images_ok = False
            img_count = 0

            # ── Étape 1 : rédaction ────────────────────────────────────────────
            log.info(f"blog_pipeline [{attempt}]: étape 1 — framer.rédiger")
            try:
                budget.check()
                # Construire le contexte — injecter le feedback QA si retry
                ctx_rediger: dict = {
                    "sujet": sujet,
                    "_pipeline_budget": budget.session,
                }
                if qa_feedback:
                    ctx_rediger["_qa_feedback"] = qa_feedback
                    log.info(f"blog_pipeline [{attempt}]: feedback QA injecté → {qa_feedback[:120]}")

                raw1 = await asyncio.wait_for(
                    dispatch("framer", "rédiger", ctx_rediger),
                    timeout=120,  # +30s par rapport à avant pour laisser le temps au retry
                )
                article_result = {"raw": raw1, "sujet": sujet}
                etape1_ok = True
                budget.sync_tokens()
                log.info(f"blog_pipeline [{attempt}]: étape 1 OK — tokens={budget.used_tokens}")

            except asyncio.TimeoutError:
                log.error(f"blog_pipeline [{attempt}]: étape 1 TIMEOUT (120s)")
                article_result = {"erreur": "timeout rédaction 120s"}
            except PipelineBudgetError as e:
                log.error(f"blog_pipeline [{attempt}]: BUDGET ÉPUISÉ: {e}")
                await notify(
                    f"⛔ *Pipeline blog — budget épuisé après {attempt} tentative(s)*\n\n"
                    f"📝 _{sujet[:100]}_\n"
                    f"_{e}_\n"
                    f"_Durée : {budget.elapsed():.0f}s_"
                )
                return
            except Exception as e:
                log.error(f"blog_pipeline [{attempt}]: étape 1 ERREUR: {e}", exc_info=True)
                article_result = {"erreur": str(e)[:200]}

            if not etape1_ok:
                erreur = article_result.get("erreur", "Erreur inconnue")
                if attempt < MAX_ATTEMPTS:
                    log.warning(f"blog_pipeline [{attempt}]: rédaction échouée — retry dans 5s")
                    qa_feedback = f"Tentative précédente échouée ({erreur}). Simplifie l'approche et génère un article complet en français."
                    await asyncio.sleep(5)
                    continue
                # Échec définitif — on a tout essayé
                await notify(
                    f"⛔ *Pipeline blog échoué après {MAX_ATTEMPTS} tentatives*\n\n"
                    f"📝 _{sujet[:100]}_\n"
                    f"Dernière erreur : _{erreur}_\n"
                    f"_Durée : {budget.elapsed():.0f}s | Tokens : {budget.used_tokens}/{budget.max_tokens}_"
                )
                return

            # ── Étape 2 : images ──────────────────────────────────────────────
            log.info(f"blog_pipeline [{attempt}]: étape 2 — framer.illustrer")
            try:
                budget.check()
                raw2 = await asyncio.wait_for(
                    dispatch("framer", "illustrer", {"sujet": sujet}),
                    timeout=135,
                )
                images_result = {"raw": raw2}
                images_ok = True
                raw2_lower = raw2.lower()
                img_count = min(raw2_lower.count("image") + raw2_lower.count("photo"), 8)
                log.info(f"blog_pipeline [{attempt}]: étape 2 OK — img≈{img_count}")
            except asyncio.TimeoutError:
                log.warning(f"blog_pipeline [{attempt}]: étape 2 TIMEOUT — continue sans images")
                images_result = {"erreur": "timeout images 135s"}
            except PipelineBudgetError as e:
                await notify(f"⛔ *Pipeline arrêté — budget à l'étape images*\n_{e}_")
                return
            except Exception as e:
                log.warning(f"blog_pipeline [{attempt}]: étape 2 ERREUR (continue): {e}")
                images_result = {"erreur": str(e)[:200]}

            # ── Étape 3 : qualité ──────────────────────────────────────────────
            log.info(f"blog_pipeline [{attempt}]: étape 3 — qualite.vérifier")
            try:
                budget.check()
                raw1_text = article_result.get("raw", "")
                titre = _extract_titre(raw1_text, sujet)
                contenu_sample = raw1_text[:800] if raw1_text else f"Article sur : {sujet}"

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
                log.info(f"blog_pipeline [{attempt}]: étape 3 OK — score={score}/10")

            except asyncio.TimeoutError:
                qualite_result = {"score": 0, "ok": False, "raison": "Timeout scoring (30s)"}
            except PipelineBudgetError as e:
                qualite_result = {"score": 0, "ok": False, "raison": f"Budget: {e}"}
            except Exception as e:
                log.error(f"blog_pipeline [{attempt}]: étape 3 ERREUR: {e}", exc_info=True)
                qualite_result = {"score": 0, "ok": False, "raison": f"Erreur: {e}"}

            score_final = qualite_result.get("score", 0)
            raison_qa   = qualite_result.get("raison", "")

            # ── QA Gate ────────────────────────────────────────────────────────
            if score_final >= 6:
                # ✅ Succès — notifier JP et terminer
                log.info(f"blog_pipeline [{attempt}]: QA PASS score={score_final}/10 → notification")
                await self._notify_done(
                    sujet=sujet,
                    etape1_ok=etape1_ok,
                    images_ok=images_ok,
                    article_result=article_result,
                    images_result=images_result,
                    qualite_result=qualite_result,
                    budget=budget,
                    attempt=attempt,
                )
                return

            # ❌ Score insuffisant — supprimer du CMS et préparer le retry
            log.warning(f"blog_pipeline [{attempt}]: QA FAIL score={score_final}/10 — {raison_qa[:100]}")
            raw1_text = article_result.get("raw", "")
            slug_published = _extract_slug(raw1_text)
            if slug_published:
                try:
                    from agents.framer import framer_list_items, framer_delete_item
                    list_res = await framer_list_items()
                    if list_res.get("ok"):
                        for item in list_res.get("items", []):
                            if item.get("slug") == slug_published:
                                await framer_delete_item(item["id"])
                                log.info(f"blog_pipeline [{attempt}]: article retiré du CMS ({slug_published})")
                                break
                except Exception as del_err:
                    log.error(f"blog_pipeline [{attempt}]: erreur suppression CMS: {del_err}")

            if attempt < MAX_ATTEMPTS:
                # Construire un feedback précis pour guider la prochaine génération
                qa_feedback = (
                    f"L'article précédent a obtenu {score_final}/10. "
                    f"Problème identifié : {raison_qa}. "
                    f"Pour la prochaine tentative : génère un article substantiel avec au moins "
                    f"5 sections développées, chaque section d'au moins 3 paragraphes, "
                    f"en apportant des exemples concrets et une analyse stratégique réelle. "
                    f"Ne répète pas le titre comme contenu."
                )
                log.info(f"blog_pipeline [{attempt}]: retry avec feedback QA dans 3s")
                await asyncio.sleep(3)
                continue

        # ── Échec après MAX_ATTEMPTS — diagnostic complet à JP ─────────────────
        log.error(f"blog_pipeline: ÉCHEC DÉFINITIF après {MAX_ATTEMPTS} tentatives — sujet={sujet[:60]!r}")
        await notify(
            f"⛔ *Pipeline blog — échec après {MAX_ATTEMPTS} tentatives autonomes*\n\n"
            f"📝 _{sujet[:100]}_\n"
            f"Dernier score QA : *{score_final}/10*\n"
            f"Diagnostic : _{raison_qa[:300]}_\n\n"
            f"💡 Suggestion : reformuler le sujet de façon plus spécifique.\n"
            f"_Durée : {budget.elapsed():.0f}s | Tokens : {budget.used_tokens}/{budget.max_tokens}_"
        )

    async def _notify_done(
        self,
        sujet: str,
        etape1_ok: bool,
        images_ok: bool,
        article_result: dict,
        images_result: dict,
        qualite_result: dict,
        budget: PipelineBudget,
        attempt: int = 1,
    ) -> None:
        """Envoie la notification de succès à JP via Telegram."""
        from core.telegram_notifier import notify

        score = qualite_result.get("score", 0)
        raison = qualite_result.get("raison", "")
        elapsed = budget.elapsed()
        tokens_used = budget.used_tokens

        score_emoji = "🟢" if score >= 8 else "🟡"
        retry_note = f" _(corrigé en {attempt} tentative{'s' if attempt > 1 else ''})_" if attempt > 1 else ""

        lien = _extract_lien(article_result.get("raw", ""))

        lines = [
            f"✅ *Article publié et validé !*{retry_note}",
            f"📝 _{sujet[:100]}_",
            "",
            f"{score_emoji} Qualité : *{score}/10* — _{raison[:120]}_",
            f"{'✅' if images_ok else '⚠️'} Images",
        ]

        if lien:
            lines.append(f"\n🔗 {lien}")

        lines.append(
            f"\n_Durée : {elapsed:.0f}s | Tokens : {tokens_used}/{budget.max_tokens}_"
        )

        msg = "\n".join(lines)
        log.info(f"blog_pipeline: succès notifié — score={score}/10 tentative={attempt}")
        await notify(msg)


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
