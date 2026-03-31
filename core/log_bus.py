"""
core/log_bus.py — Bus de logs en mémoire pour le dashboard web.

Capture tous les logs Python en temps réel et les rend accessibles
via SSE (Server-Sent Events) depuis l'API FastAPI.

Thread-safety : asyncio.Queue + loop.call_soon_threadsafe pour
injecter les logs depuis le thread du bot Telegram vers l'event loop uvicorn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import AsyncGenerator

# ── Entrée de log ─────────────────────────────────────────────────────────────

class LogEntry:
    __slots__ = ("id", "ts", "level", "agent", "msg", "exc")

    def __init__(self, id_: int, record: logging.LogRecord):
        self.id    = id_
        self.ts    = round(record.created, 3)
        self.level = record.levelname
        self.agent = record.name
        self.msg   = record.getMessage()
        self.exc   = ""
        if record.exc_info:
            self.exc = logging.Formatter().formatException(record.exc_info)

    def to_dict(self) -> dict:
        d = {
            "id":    self.id,
            "ts":    self.ts,
            "level": self.level,
            "agent": self.agent,
            "msg":   self.msg,
        }
        if self.exc:
            d["exc"] = self.exc
        return d


# ── Singleton LogBus ──────────────────────────────────────────────────────────

class LogBus:
    """
    Bus central de logs en mémoire.
    - Stocke les 500 dernières entrées dans une deque
    - Notifie les abonnés SSE via asyncio.Queue
    """

    def __init__(self, maxlen: int = 500):
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)
        self._counter: int             = 0
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Enregistre l'event loop uvicorn pour les cross-thread pushes."""
        self._loop = loop

    def push(self, record: logging.LogRecord) -> None:
        """
        Appelé depuis n'importe quel thread (LogBusHandler).
        Thread-safe via call_soon_threadsafe.
        """
        self._counter += 1
        entry = LogEntry(self._counter, record)
        self._entries.append(entry)

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._notify_subscribers, entry)

    def _notify_subscribers(self, entry: LogEntry) -> None:
        """Appelé dans l'event loop uvicorn — put_nowait est sûr ici."""
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # Abonné lent — on drop l'entrée

    def tail(self, n: int = 50) -> list[dict]:
        """Retourne les N dernières entrées."""
        entries = list(self._entries)
        return [e.to_dict() for e in entries[-n:]]

    async def stream(self, since_id: int = 0) -> AsyncGenerator[LogEntry, None]:
        """
        Générateur async — yield les nouvelles entrées en temps réel.
        D'abord les entrées manquées depuis since_id, puis les nouvelles.
        """
        # 1. Entrées manquées depuis since_id
        for entry in list(self._entries):
            if entry.id > since_id:
                yield entry

        # 2. Nouvelles entrées via Queue
        q: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        try:
            while True:
                entry = await q.get()
                yield entry
        finally:
            self._subscribers.remove(q)


# ── Instance globale ──────────────────────────────────────────────────────────
bus = LogBus()


# ── Handler Python logging ────────────────────────────────────────────────────

class LogBusHandler(logging.Handler):
    """
    Handler logging qui alimente le bus en mémoire.
    S'installe en plus du StreamHandler existant.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            bus.push(record)
        except Exception:
            pass  # Jamais crasher le logging


# ── Installation ──────────────────────────────────────────────────────────────

def install_log_bus() -> None:
    """
    À appeler au démarrage (main.py) AVANT setup_logging().
    Installe le LogBusHandler sur le root logger.
    """
    handler = LogBusHandler()
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
