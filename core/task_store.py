"""
core/task_store.py — Store in-memory thread-safe des tâches parallèles.

Garde au max 50 tâches en mémoire (FIFO).
Toutes les opérations sont protégées par threading.Lock.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

MAX_TASKS_IN_MEMORY = 50


@dataclass
class Task:
    id: str
    agent: str
    command: str
    context: dict
    sujet: str          # label lisible pour JP
    status: str         # "queued" | "running" | "done" | "failed" | "budget_exceeded"
    result: Optional[str] = None
    error: Optional[str] = None
    tokens_used: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notion_url: Optional[str] = None


class TaskStore:
    """Store in-memory thread-safe des tâches."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []   # FIFO pour la rotation
        self._lock = threading.Lock()

    def add(self, task: Task) -> str:
        """Ajoute une tâche et retourne son task_id. Évince les plus vieilles si > 50."""
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            # Rotation FIFO — garder max 50 tâches
            while len(self._order) > MAX_TASKS_IN_MEMORY:
                oldest_id = self._order.pop(0)
                self._tasks.pop(oldest_id, None)
        return task.id

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs) -> None:
        """Met à jour les champs d'une tâche existante."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)

    def all_tasks(self) -> list[Task]:
        """Retourne toutes les tâches dans l'ordre d'insertion (plus récentes en dernier)."""
        with self._lock:
            return [self._tasks[tid] for tid in self._order if tid in self._tasks]

    def active_count(self) -> int:
        """Nombre de tâches queued ou running."""
        with self._lock:
            return sum(
                1 for t in self._tasks.values()
                if t.status in ("queued", "running")
            )

    def session_tokens_used(self) -> int:
        """Somme des tokens de toutes les tâches (running + done + failed)."""
        with self._lock:
            return sum(t.tokens_used for t in self._tasks.values())

    def status_report(self) -> str:
        """Retourne un rapport formaté pour Telegram (Markdown)."""
        with self._lock:
            tasks = [self._tasks[tid] for tid in self._order if tid in self._tasks]

        running  = [t for t in tasks if t.status == "running"]
        queued   = [t for t in tasks if t.status == "queued"]
        done     = [t for t in tasks if t.status == "done"]
        failed   = [t for t in tasks if t.status in ("failed", "budget_exceeded")]

        # Dernières 5 tâches terminées seulement (plus lisible)
        done_recent  = done[-5:]
        failed_recent = failed[-5:]

        lines = ["*Status des tâches*\n"]

        # En cours
        in_progress = running + queued
        if in_progress:
            lines.append(f"En cours ({len(in_progress)}):")
            for t in in_progress:
                icon = ">" if t.status == "running" else "."
                lines.append(f"  {icon} {t.sujet} — {t.agent}.{t.command}")
        else:
            lines.append("En cours: aucune")

        # Terminés
        if done_recent:
            lines.append(f"\nTermines ({len(done)}):")
            for t in done_recent:
                notion_link = f" — [Notion]({t.notion_url})" if t.notion_url else ""
                lines.append(f"  + {t.sujet}{notion_link}")
        else:
            lines.append("\nTermines: aucune")

        # Echecs
        if failed_recent:
            lines.append(f"\nEchoues ({len(failed)}):")
            for t in failed_recent:
                reason = t.error or t.status
                # Tronquer la raison
                if len(reason) > 60:
                    reason = reason[:60] + "..."
                lines.append(f"  x {t.sujet} — {reason}")

        # Tokens — on importe GLOBAL_SESSION_BUDGET tardivement pour éviter circulaire
        total_tokens = sum(t.tokens_used for t in tasks)
        try:
            from core.task_manager import GLOBAL_SESSION_BUDGET as _global_budget
        except ImportError:
            _global_budget = 50_000
        lines.append(f"\nTokens session: {total_tokens:,} / {_global_budget:,}")

        return "\n".join(lines)


def make_task(
    agent: str,
    command: str,
    context: dict,
    sujet: str,
) -> Task:
    """Factory qui crée une Task avec un UUID et le statut 'queued'."""
    return Task(
        id=str(uuid.uuid4()),
        agent=agent,
        command=command,
        context=context,
        sujet=sujet,
        status="queued",
    )
