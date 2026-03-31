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
        Orchestre les 3 étapes séquentiellement.
        Toutes les erreurs sont attrapées — ne remonte jamais d'exception.
        """
        from core.telegram_notifier import notify
        from core.dispatcher import dispatch

        budget = PipelineBudget()
        budget.start()

        # Résultats inter-étapes
        article_result: dict = {}
        images_result: dict = {}
        qualite_result: dict = {}

        # ── Étape 1 : rédaction ────────────────────────────────────────────────
        log.info("blog_pipeline: étape 1/3 — framer.rédiger")
        etape1_ok = False
        try:
            budget.check()
            raw1 = await asyncio.wait_for(
                dispatch("framer", "rédiger", {"sujet": sujet, "_pipeline_budget": budget.session}),
                timeout=90,
            )
            # framer.rédiger retourne une str avec le résultat
            # On essaie de détecter le slug/lien dans la réponse
            article_result = {"raw": raw1, "sujet": sujet}
            etape1_ok = True
            budget.sync_tokens()
            log.info(
                f"blog_pipeline: étape 1/3 OK — "
                f"tokens={budget.used_tokens} elapsed={budget.elapsed():.1f}s"
            )
        except asyncio.TimeoutError:
            log.error("blog_pipeline: étape 1/3 TIMEOUT (90s)")
            article_result = {"raw": "", "sujet": sujet, "erreur": "timeout rédaction 90s"}
        except PipelineBudgetError as e:
            log.error(f"blog_pipeline: étape 1/3 BUDGET: {e}")
            await notify(
                f"⛔ *Pipeline blog arrêté* — budget dépassé à l'étape 1\n_{e}_"
            )
            return
        except Exception as e:
            log.error(f"blog_pipeline: étape 1/3 ERREUR: {e}", exc_info=True)
            article_result = {"raw": "", "sujet": sujet, "erreur": str(e)}

        # ── Étape 2 : images ──────────────────────────────────────────────────
        log.info("blog_pipeline: étape 2/3 — framer.illustrer")
        images_ok = False
        img_count = 0
        try:
            budget.check()

            # illustrer n'a pas de timeout propre → on en impose un
            raw2 = await asyncio.wait_for(
                dispatch("framer", "illustrer", {"sujet": sujet}),
                timeout=135,  # 3 images × 45s
            )
            images_result = {"raw": raw2}
            images_ok = True

            # Compter les images (heuristique : occurrences de "image" dans la réponse)
            raw2_lower = raw2.lower()
            img_count = raw2_lower.count("image") + raw2_lower.count("photo")
            img_count = min(img_count, 8)  # plafond raisonnable

            log.info(
                f"blog_pipeline: étape 2/3 OK — "
                f"img_count≈{img_count} elapsed={budget.elapsed():.1f}s"
            )
        except asyncio.TimeoutError:
            log.warning("blog_pipeline: étape 2/3 TIMEOUT (135s) — continue sans images")
            images_result = {"raw": "", "erreur": "timeout images 135s"}
        except PipelineBudgetError as e:
            log.error(f"blog_pipeline: étape 2/3 BUDGET: {e}")
            await notify(
                f"⛔ *Pipeline blog arrêté* — budget dépassé à l'étape 2\n_{e}_"
            )
            return
        except Exception as e:
            log.warning(f"blog_pipeline: étape 2/3 ERREUR (continue): {e}")
            images_result = {"raw": "", "erreur": str(e)}

        # ── Étape 3 : qualité ─────────────────────────────────────────────────
        log.info("blog_pipeline: étape 3/3 — qualite.vérifier")
        try:
            budget.check()

            # Extraire titre + extrait du résultat étape 1
            raw1_text = article_result.get("raw", "")
            titre = _extract_titre(raw1_text, sujet)
            contenu_sample = raw1_text[:500] if raw1_text else f"Article sur : {sujet}"

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
            log.info(
                f"blog_pipeline: étape 3/3 OK — "
                f"score={qualite_result.get('score')}/10 "
                f"elapsed={budget.elapsed():.1f}s tokens={budget.used_tokens}"
            )
        except asyncio.TimeoutError:
            log.error("blog_pipeline: étape 3/3 TIMEOUT (30s)")
            qualite_result = {
                "score": 0,
                "ok": False,
                "raison": "Timeout scoring (30s)",
                "details": "",
            }
        except PipelineBudgetError as e:
            log.error(f"blog_pipeline: étape 3/3 BUDGET: {e}")
            qualite_result = {
                "score": 0,
                "ok": False,
                "raison": f"Budget dépassé avant scoring : {e}",
                "details": "",
            }
        except Exception as e:
            log.error(f"blog_pipeline: étape 3/3 ERREUR: {e}", exc_info=True)
            qualite_result = {
                "score": 0,
                "ok": False,
                "raison": f"Erreur scoring : {e}",
                "details": "",
            }

        # ── Notification finale ───────────────────────────────────────────────
        await self._notify_done(
            sujet=sujet,
            etape1_ok=etape1_ok,
            images_ok=images_ok,
            article_result=article_result,
            images_result=images_result,
            qualite_result=qualite_result,
            budget=budget,
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
    ) -> None:
        """Envoie la notification de fin à JP via Telegram."""
        from core.telegram_notifier import notify

        score = qualite_result.get("score", 0)
        ok = qualite_result.get("ok", False)
        raison = qualite_result.get("raison", "")
        elapsed = budget.elapsed()
        tokens_used = budget.used_tokens

        # Emoji score
        if score >= 8:
            score_emoji = "🟢"
        elif score >= 6:
            score_emoji = "🟡"
        else:
            score_emoji = "🔴"

        # Extraire le lien Framer si présent dans le résultat étape 1
        lien = _extract_lien(article_result.get("raw", ""))

        lines = [
            f"✅ *Pipeline blog terminé*",
            f"📝 Sujet : _{sujet[:100]}_",
            "",
            f"{'✅' if etape1_ok else '❌'} Rédaction",
            f"{'✅' if images_ok else '⚠️'} Images",
            f"{score_emoji} Qualité : *{score}/10* — _{raison[:120]}_",
        ]

        if lien:
            lines.append(f"\n🔗 {lien}")

        if not ok:
            lines.append(
                f"\n⚠️ *Score < 6/10* — l'article a été publié mais mérite une révision manuelle."
            )

        lines.append(
            f"\n_Durée : {elapsed:.0f}s | Tokens : {tokens_used}/{budget.max_tokens}_"
        )

        msg = "\n".join(lines)
        log.info(f"blog_pipeline: notification finale envoyée — score={score}/10")
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


agent = BlogPipelineAgent()
