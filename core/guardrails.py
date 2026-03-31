"""
core/guardrails.py — Garde-fous token et timeout pour tous les appels Claude.

Chaque appel API passe par safe_claude_call() qui garantit :
  - Timeout dur configurable (défaut 90s)
  - Budget token par session
  - Détection de boucle (hash des réponses successives)
  - Logging automatique de la consommation

Variables Railway :
  CLAUDE_CALL_TIMEOUT_S   = timeout par appel (défaut 90)
  CLAUDE_SESSION_BUDGET   = tokens max par session (défaut 50 000)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

# ── Config depuis env vars ─────────────────────────────────────────────────────
CALL_TIMEOUT_S    = int(os.environ.get("CLAUDE_CALL_TIMEOUT_S", "90"))
SESSION_BUDGET    = int(os.environ.get("CLAUDE_SESSION_BUDGET", "50000"))
LOOP_WINDOW       = 3   # nb de réponses à comparer pour détecter une boucle


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Levée quand le budget token de la session est épuisé."""

class CallTimeoutError(Exception):
    """Levée quand un appel Claude dépasse le timeout configuré."""

class LoopDetectedError(Exception):
    """Levée quand l'agent répète la même réponse en boucle."""


# ── Session budget (par requête Telegram / Dashboard) ─────────────────────────

class SessionBudget:
    """Compteur de tokens consommés pour une session utilisateur."""

    def __init__(self, limit: int = SESSION_BUDGET):
        self.limit        = limit
        self.input_tokens = 0
        self.output_tokens = 0
        self._created_at  = time.time()

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.total)

    def record(self, usage) -> None:
        """Enregistre l'usage retourné par l'API Anthropic."""
        if usage is None:
            return
        self.input_tokens  += getattr(usage, "input_tokens",  0)
        self.output_tokens += getattr(usage, "output_tokens", 0)
        log.info(
            f"guardrails: tokens session — "
            f"in={self.input_tokens} out={self.output_tokens} "
            f"total={self.total}/{self.limit}"
        )

    def check(self) -> None:
        """Lève BudgetExceededError si le budget est épuisé."""
        if self.total >= self.limit:
            raise BudgetExceededError(
                f"Budget session épuisé ({self.total}/{self.limit} tokens). "
                "Nouvelle session nécessaire."
            )


# ── Détecteur de boucle ───────────────────────────────────────────────────────

class LoopDetector:
    """Détecte quand un agent retourne les mêmes réponses en boucle."""

    def __init__(self, window: int = LOOP_WINDOW):
        self._hashes: deque[str] = deque(maxlen=window)

    def check(self, text: str) -> None:
        """
        Lève LoopDetectedError si le texte (ou une version très similaire)
        a déjà été retourné dans les dernières `window` réponses.
        """
        h = hashlib.md5(text.strip()[:500].encode()).hexdigest()
        if h in self._hashes:
            raise LoopDetectedError(
                "Boucle détectée — l'agent répète la même réponse. "
                "Arrêt automatique pour préserver les tokens."
            )
        self._hashes.append(h)


# ── Appel Claude sécurisé ─────────────────────────────────────────────────────

async def safe_claude_call(
    client,
    *,
    model: str,
    max_tokens: int,
    messages: list[dict],
    system: str | None = None,
    tools: list[dict] | None = None,
    timeout_s: int | None = None,
    budget: SessionBudget | None = None,
    loop_detector: LoopDetector | None = None,
    agent_name: str = "unknown",
) -> Any:
    """
    Appel Claude avec timeout dur, budget token et détection de boucle.

    Args:
        client:         Instance anthropic.Anthropic
        model:          Modèle Claude (ex: "claude-sonnet-4-6")
        max_tokens:     Tokens max pour cette réponse
        messages:       Historique de conversation
        system:         System prompt optionnel
        tools:          Outils Claude optionnels
        timeout_s:      Timeout en secondes (défaut: CALL_TIMEOUT_S)
        budget:         SessionBudget — si fourni, vérifié et mis à jour
        loop_detector:  LoopDetector — si fourni, vérifie les boucles
        agent_name:     Nom de l'agent (pour les logs)

    Returns:
        Objet response Anthropic complet

    Raises:
        BudgetExceededError: budget épuisé
        CallTimeoutError:    timeout dépassé
        LoopDetectedError:   boucle détectée
    """
    if budget:
        budget.check()

    t = timeout_s or CALL_TIMEOUT_S
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools

    start = time.time()
    log.info(
        f"guardrails [{agent_name}]: appel Claude — "
        f"model={model} max_tokens={max_tokens} timeout={t}s"
    )

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(client.messages.create, **kwargs),
            timeout=t,
        )
    except asyncio.TimeoutError:
        elapsed = round(time.time() - start, 1)
        log.error(
            f"guardrails [{agent_name}]: TIMEOUT après {elapsed}s "
            f"(limite={t}s)"
        )
        raise CallTimeoutError(
            f"⏱️ L'agent {agent_name} a dépassé {t}s. "
            "Opération annulée automatiquement."
        )

    elapsed = round(time.time() - start, 1)

    # Mise à jour budget
    if budget and hasattr(resp, "usage"):
        budget.record(resp.usage)

    # Détection de boucle sur le premier bloc texte
    if loop_detector:
        text = ""
        for block in (resp.content or []):
            if hasattr(block, "text"):
                text = block.text
                break
        if text:
            loop_detector.check(text)

    log.info(
        f"guardrails [{agent_name}]: réponse reçue en {elapsed}s — "
        f"stop_reason={resp.stop_reason}"
    )
    return resp
