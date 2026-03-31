"""
core/task_manager.py — Orchestrateur de tâches parallèles avec guardrails stricts.

GUARDRAILS NON NÉGOCIABLES:
  TASK_TOKEN_BUDGET    = 15 000 tokens max par tâche individuelle
  TASK_TIMEOUT_S       = 240 secondes max par tâche
  MAX_CONCURRENT_TASKS = 5 tâches parallèles max
  GLOBAL_SESSION_BUDGET = 50 000 tokens total pour toutes les tâches actives

- Si une tâche dépasse son budget → stop + notif Telegram, les autres continuent
- Si budget global dépassé → refuser les nouvelles tâches, notifier JP
- Aucune tâche ne peut spawner d'autres tâches
- Max 1 retry par tâche (uniquement si erreur réseau)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.task_store import Task, TaskStore, make_task

log = logging.getLogger(__name__)

# ── Guardrails — NON NÉGOCIABLES ───────────────────────────────────────────────
TASK_TOKEN_BUDGET     = 15_000
TASK_TIMEOUT_S        = 240
MAX_CONCURRENT_TASKS  = 5
GLOBAL_SESSION_BUDGET = 50_000

# Erreurs réseau → eligible pour retry (1 max)
_NETWORK_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,
)


class TaskManager:
    """Orchestrateur principal des tâches parallèles."""

    def __init__(self, store: TaskStore, notifier, notion_agent=None):
        """
        Args:
            store:        TaskStore instance
            notifier:     callable async notify(text) → bool (ex: telegram_notifier.notify)
            notion_agent: agent Notion (optionnel) pour push des résultats
        """
        self.store        = store
        self.notifier     = notifier
        self.notion_agent = notion_agent
        self._semaphore   = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    async def queue_tasks(self, tasks: list[dict], chat_id: int) -> str:
        """
        Reçoit une liste de tâches, vérifie le budget global,
        lance tout en parallèle via asyncio.gather,
        retourne immédiatement un message de confirmation.
        """
        if not tasks:
            return "Aucune tâche a lancer."

        # Vérifier le budget global AVANT de lancer
        session_tokens = self.store.session_tokens_used()
        if session_tokens >= GLOBAL_SESSION_BUDGET:
            msg = (
                f"Budget global epuise ({session_tokens:,}/{GLOBAL_SESSION_BUDGET:,} tokens). "
                f"Nouvelles taches refusees. Relance une nouvelle session."
            )
            await self.notifier(msg)
            return msg

        # Vérifier le nombre de tâches actives
        active = self.store.active_count()
        if active >= MAX_CONCURRENT_TASKS:
            msg = (
                f"Trop de taches actives ({active}/{MAX_CONCURRENT_TASKS}). "
                f"Attends que des taches se terminent avant d'en lancer de nouvelles."
            )
            return msg

        # Créer les Task objects
        task_objects: list[Task] = []
        for t in tasks:
            task = make_task(
                agent=t.get("agent", "chat"),
                command=t.get("command", "respond"),
                context=t.get("context", {}),
                sujet=t.get("sujet", t.get("agent", "tache")),
            )
            self.store.add(task)
            task_objects.append(task)

        n = len(task_objects)
        sujets = ", ".join(t.sujet for t in task_objects)
        confirm = f"Lancement de {n} tache(s) en parallele:\n{sujets}\n\nJe te notifie a la fin de chacune."

        # Lancer en arrière-plan (fire-and-forget) — ne pas await gather ici
        asyncio.create_task(
            self._run_all(task_objects, chat_id),
            name=f"tasks-batch-{task_objects[0].id[:8]}",
        )

        return confirm

    async def _run_all(self, tasks: list[Task], chat_id: int) -> None:
        """Lance toutes les tâches en parallèle via asyncio.gather."""
        await asyncio.gather(
            *[self._run_task_guarded(t) for t in tasks],
            return_exceptions=True,  # une exception ne tue pas les autres
        )

    async def _run_task_guarded(self, task: Task) -> None:
        """Acquire le semaphore puis exécute la tâche."""
        async with self._semaphore:
            await self._run_task(task)

    async def _run_task(self, task: Task) -> None:
        """
        Exécute une tâche unique avec:
        - asyncio.wait_for(timeout=TASK_TIMEOUT_S)
        - SessionBudget(max_tokens=TASK_TOKEN_BUDGET)
        - try/except pour BudgetExceededError, TimeoutError, Exception
        - Après succès: push Notion + notif Telegram
        - Après échec: notif Telegram avec raison
        Max 1 retry si erreur réseau.
        """
        from core.guardrails import SessionBudget, BudgetExceededError

        self.store.update(task.id, status="running", started_at=datetime.utcnow())
        log.info(f"task_manager: démarrage task={task.id} {task.agent}.{task.command} sujet={task.sujet}")

        budget = SessionBudget(limit=TASK_TOKEN_BUDGET)
        retried = False

        async def _execute():
            """Exécution réelle de la tâche avec budget guardrail."""
            from core.dispatcher import dispatch

            # Vérifier le budget global avant chaque exécution
            session_tokens = self.store.session_tokens_used()
            if session_tokens >= GLOBAL_SESSION_BUDGET:
                raise BudgetExceededError(
                    f"Budget global session epuise ({session_tokens:,}/{GLOBAL_SESSION_BUDGET:,} tokens)"
                )

            # Injecter le budget dans le context pour que les agents puissent l'utiliser
            ctx = dict(task.context)
            ctx["_task_budget"] = budget

            result = await dispatch(task.agent, task.command, ctx)
            return result

        for attempt in range(2):  # max 2 tentatives (1 original + 1 retry)
            try:
                result = await asyncio.wait_for(
                    _execute(),
                    timeout=TASK_TIMEOUT_S,
                )

                # Succès
                tokens = budget.total
                self.store.update(
                    task.id,
                    status="done",
                    result=result,
                    tokens_used=tokens,
                    completed_at=datetime.utcnow(),
                )
                log.info(f"task_manager: tâche terminée task={task.id} tokens={tokens}")

                # Push Notion
                notion_url = await self._push_to_notion(task, result)
                if notion_url:
                    self.store.update(task.id, notion_url=notion_url)

                # Notif Telegram
                notion_link = f" — [Notion]({notion_url})" if notion_url else ""
                await self.notifier(
                    f"Tache terminee: *{task.sujet}*{notion_link}\n"
                    f"Tokens utilises: {tokens:,}/{TASK_TOKEN_BUDGET:,}"
                )
                return  # Succès — sortir

            except asyncio.TimeoutError:
                tokens = budget.total
                self.store.update(
                    task.id,
                    status="failed",
                    error=f"timeout ({TASK_TIMEOUT_S}s)",
                    tokens_used=tokens,
                    completed_at=datetime.utcnow(),
                )
                log.error(f"task_manager: TIMEOUT task={task.id} apres {TASK_TIMEOUT_S}s")
                await self.notifier(
                    f"Tache echouee (timeout {TASK_TIMEOUT_S}s): *{task.sujet}*\n"
                    f"L'agent {task.agent}.{task.command} n'a pas repondu a temps."
                )
                return  # Pas de retry pour timeout

            except BudgetExceededError as e:
                tokens = budget.total
                self.store.update(
                    task.id,
                    status="budget_exceeded",
                    error=str(e),
                    tokens_used=tokens,
                    completed_at=datetime.utcnow(),
                )
                log.error(f"task_manager: BUDGET task={task.id} tokens={tokens}")
                await self.notifier(
                    f"Tache arretee (budget depasse): *{task.sujet}*\n"
                    f"Tokens utilises: {tokens:,}/{TASK_TOKEN_BUDGET:,}\n"
                    f"Les autres taches continuent."
                )
                return  # Pas de retry pour dépassement budget

            except _NETWORK_ERRORS as e:
                if attempt == 0 and not retried:
                    # 1 seul retry pour erreur réseau
                    retried = True
                    log.warning(f"task_manager: erreur reseau task={task.id}, retry... ({e})")
                    await asyncio.sleep(2)
                    continue
                else:
                    # Deuxième échec → failed définitif
                    tokens = budget.total
                    self.store.update(
                        task.id,
                        status="failed",
                        error=f"erreur reseau apres retry: {e}",
                        tokens_used=tokens,
                        completed_at=datetime.utcnow(),
                    )
                    log.error(f"task_manager: FAILED (reseau) task={task.id}: {e}")
                    await self.notifier(
                        f"Tache echouee (reseau): *{task.sujet}*\n"
                        f"Erreur: {str(e)[:100]}"
                    )
                    return

            except Exception as e:
                tokens = budget.total
                self.store.update(
                    task.id,
                    status="failed",
                    error=str(e)[:200],
                    tokens_used=tokens,
                    completed_at=datetime.utcnow(),
                )
                log.error(f"task_manager: FAILED task={task.id}: {e}", exc_info=True)
                await self.notifier(
                    f"Tache echouee: *{task.sujet}*\n"
                    f"Erreur: {str(e)[:150]}"
                )
                return  # Pas de retry pour erreur logique

    async def _push_to_notion(self, task: Task, result: str) -> Optional[str]:
        """
        Stocke le résultat dans Notion via l'agent notion.store_output().
        Retourne l'URL Notion ou None si échec.
        Budget: 0 appels Claude. Timeout: 15s.
        """
        if self.notion_agent is None:
            return None

        try:
            ctx = {
                "titre": task.sujet,
                "contenu": result[:5000],  # Limiter la taille
                "type": task.command,
                "source_agent": task.agent,
            }
            url = await asyncio.wait_for(
                self.notion_agent.store_output(ctx),
                timeout=15,
            )
            log.info(f"task_manager: résultat push Notion task={task.id} url={url}")
            return url
        except asyncio.TimeoutError:
            log.warning(f"task_manager: Notion push timeout task={task.id}")
            return None
        except Exception as e:
            log.warning(f"task_manager: Notion push failed task={task.id}: {e}")
            return None

    def get_status_report(self) -> str:
        return self.store.status_report()
