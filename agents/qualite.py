"""
agents/qualite.py — Agent de scoring qualité pour les articles de blog.

Architecture : 1 seul appel Claude, retourne un JSON score.
Budget : 500 tokens max (≈200 input + 300 output).
Timeout : 30s dur.

Utilisé exclusivement par blog_pipeline.py — jamais appelé directement.
"""
from __future__ import annotations

import json
import logging

from agents._base import BaseAgent
from core.brain import get_client
from core.guardrails import safe_claude_call, SessionBudget
from config import CLAUDE_MODEL

log = logging.getLogger(__name__)

# ── Prompt qualité ─────────────────────────────────────────────────────────────
_QUALITY_PROMPT = """\
Tu es un éditeur de blog. Évalue cet article en 1 phrase et donne un score /10.
Titre: {titre}
Sujet demandé: {sujet}
Extrait (500 chars): {contenu_sample}
Images: {img_count}

Retourne UNIQUEMENT ce JSON:
{{"score": 7, "ok": true, "raison": "Titre accrocheur, contenu pertinent pour PME québécoises"}}\
"""

_TIMEOUT_S = 30
_MAX_TOKENS = 300


class QualiteAgent(BaseAgent):
    name = "qualite"
    description = "Scoring qualité d'article de blog (pipeline interne)"
    schedules: list = []

    @property
    def commands(self):
        return {"vérifier": self.verifier}

    async def verifier(self, context: dict | None = None) -> str:
        """Wrapper run_command — retourne le JSON sérialisé."""
        ctx = context or {}
        result = await self.verifier_article(ctx)
        return json.dumps(result, ensure_ascii=False)

    async def verifier_article(
        self,
        context: dict,
        budget: SessionBudget | None = None,
    ) -> dict:
        """
        Évalue la qualité d'un article de blog.

        Args:
            context: {
                "titre": str,
                "contenu_sample": str  (≤500 chars),
                "sujet": str,
                "img_count": int,
            }
            budget: SessionBudget externe (facultatif)

        Returns:
            {"score": N, "ok": bool, "raison": str, "details": str}
        """
        titre = context.get("titre", "")
        contenu_sample = str(context.get("contenu_sample", ""))[:500]
        sujet = context.get("sujet", "")
        img_count = context.get("img_count", 0)

        prompt = _QUALITY_PROMPT.format(
            titre=titre,
            sujet=sujet,
            contenu_sample=contenu_sample,
            img_count=img_count,
        )

        log.info(f"qualite: évaluation article — titre={titre[:60]!r}")

        try:
            resp = await safe_claude_call(
                get_client(),
                model=CLAUDE_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                timeout_s=_TIMEOUT_S,
                budget=budget,
                agent_name="qualite.vérifier",
            )
            raw = resp.content[0].text.strip()

            # Nettoyer si Claude entoure de markdown
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            score = int(data.get("score", 5))
            ok = bool(data.get("ok", score >= 6))
            raison = str(data.get("raison", ""))
            details = str(data.get("details", raison))

            log.info(f"qualite: score={score}/10 ok={ok} — {raison[:80]}")
            return {"score": score, "ok": ok, "raison": raison, "details": details}

        except json.JSONDecodeError as e:
            log.error(f"qualite: JSON parse error: {e}")
            return {
                "score": 5,
                "ok": False,
                "raison": "Impossible de parser la réponse du scoreur",
                "details": str(e),
            }
        except Exception as e:
            log.error(f"qualite: erreur: {e}")
            return {
                "score": 0,
                "ok": False,
                "raison": f"Erreur scoring: {e}",
                "details": str(e),
            }


agent = QualiteAgent()
