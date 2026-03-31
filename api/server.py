"""
api/server.py — Serveur HTTP FastAPI pour Paperclip HTTP Adapter + Dashboard web.

Expose chaque agent comme endpoint REST que Paperclip peut appeler.
Tourne en parallèle du bot Telegram sur le port $PORT (Railway) ou 8080.

Format Paperclip HTTP Adapter:
  POST /agents/{name}/{command}
  Body: { "runId": "...", "agentId": "...", "taskId": "...", "context": {...} }
  Returns: { "status": "success"|"error", "result": "...", "usage": {...} }

Dashboard web:
  GET  /          → dashboard/index.html
  POST /dashboard/command → SSE stream
  GET  /logs      → JSON snapshot
  GET  /logs/stream → SSE temps réel
"""
import asyncio
import json
import os
import time
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"

# ── Pont Telegram ↔ API : event loop principal exposé pour queue_tasks ────────
# Initialisé par bot/telegram.py via set_main_loop() au démarrage
_main_loop: asyncio.AbstractEventLoop | None = None
_task_manager_ref = None   # référence au TaskManager du bot Telegram

def set_main_loop(loop: asyncio.AbstractEventLoop, task_manager) -> None:
    """Appelé par bot/telegram.py pour exposer le loop et le task_manager à l'API."""
    global _main_loop, _task_manager_ref
    _main_loop = loop
    _task_manager_ref = task_manager

app = FastAPI(
    title="Welldone AI Agents",
    description="HTTP Adapter + Dashboard — Welldone Studio",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Secrets
WEBHOOK_SECRET   = os.environ.get("PAPERCLIP_WEBHOOK_SECRET", "")
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_secret(authorization: str = Header(default="")):
    """Vérifie le Bearer token si PAPERCLIP_WEBHOOK_SECRET est configuré."""
    if not WEBHOOK_SECRET:
        return
    token = authorization.replace("Bearer ", "").strip()
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")


def verify_dashboard(request: Request, authorization: str = Header(default="")):
    """Auth dashboard — Bearer header ou ?token= query param."""
    if not DASHBOARD_SECRET:
        return  # Open (dev local ou pas de secret configuré)
    token = (
        authorization.replace("Bearer ", "").strip()
        or request.query_params.get("token", "")
    )
    if token != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="Token invalide")


# ── Modèles ───────────────────────────────────────────────────────────────────

class PaperclipPayload(BaseModel):
    runId: str | None = None
    agentId: str | None = None
    taskId: str | None = None
    wakeReason: str | None = "task_assigned"
    context: dict[str, Any] | None = None


class PaperclipResponse(BaseModel):
    status: str
    result: str
    usage: dict[str, Any] | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — Railway + Paperclip."""
    from core.dispatcher import REGISTRY, discover_agents
    if not REGISTRY:
        discover_agents()
    return {
        "status": "ok",
        "agents": list(REGISTRY.keys()),
        "timestamp": time.time(),
    }


@app.get("/agents")
async def list_agents(_=Depends(verify_secret)):
    """Liste tous les agents et leurs commandes disponibles."""
    from core.dispatcher import REGISTRY, discover_agents
    if not REGISTRY:
        discover_agents()
    return {
        name: {
            "description": agent.description,
            "commands": list(agent.commands.keys()),
            "schedules": agent.schedules,
        }
        for name, agent in REGISTRY.items()
    }


@app.post("/agents/{agent_name}/{command}", response_model=PaperclipResponse)
async def run_agent(
    agent_name: str,
    command: str,
    payload: PaperclipPayload,
    _=Depends(verify_secret),
):
    """
    Endpoint principal Paperclip HTTP Adapter.
    Paperclip appelle cet endpoint lors d'un heartbeat ou d'une tâche assignée.
    """
    from core.dispatcher import dispatch

    start = time.time()
    log.info(f"api: Paperclip → {agent_name}.{command} | task={payload.taskId} | reason={payload.wakeReason}")

    context = payload.context or {}

    try:
        result = await dispatch(agent_name, command, context if context else None)
        elapsed = round(time.time() - start, 2)
        log.info(f"api: {agent_name}.{command} → succès en {elapsed}s")
        return PaperclipResponse(
            status="success",
            result=result,
            usage={"duration_seconds": elapsed, "agent": agent_name, "command": command},
        )
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        log.error(f"api: {agent_name}.{command} → erreur: {e}")
        return PaperclipResponse(
            status="error",
            result=f"❌ Erreur {agent_name}.{command}: {e}",
            usage={"duration_seconds": elapsed, "agent": agent_name, "command": command},
        )


# ── Paperclip Task Queue ──────────────────────────────────────────────────────

class TaskQueuePayload(BaseModel):
    tasks: list[dict]          # [{agent, command, context, sujet}]
    chat_id: int | None = None # optionnel, défaut = TELEGRAM_ALLOWED_USER_ID

class TaskQueueResponse(BaseModel):
    status: str
    queued: int
    message: str
    task_ids: list[str] = []


@app.post("/tasks/queue", response_model=TaskQueueResponse)
async def queue_tasks(payload: TaskQueuePayload, _=Depends(verify_secret)):
    """
    Paperclip soumet 1 ou N tâches → exécution parallèle fire-and-forget.
    Les résultats sont poussés dans Notion + notifiés via Telegram.
    Guardrails : max 5 tâches, 50k tokens global, 15k/tâche, 240s/tâche.
    """
    from config import TELEGRAM_ALLOWED_USER_ID
    from core.task_store import make_task

    if not _task_manager_ref or not _main_loop:
        raise HTTPException(status_code=503, detail="TaskManager non initialisé — bot Telegram pas encore démarré")

    if len(payload.tasks) > 5:
        raise HTTPException(status_code=400, detail="Max 5 tâches par requête")

    chat_id = payload.chat_id or TELEGRAM_ALLOWED_USER_ID
    tasks = [
        make_task(
            agent=t.get("agent", "chat"),
            command=t.get("command", "respond"),
            context=t.get("context", {}),
            sujet=t.get("sujet", f"{t.get('agent')}.{t.get('command')}"),
        )
        for t in payload.tasks
    ]

    # Soumettre au TaskManager via le loop principal (thread-safe)
    future = asyncio.run_coroutine_threadsafe(
        _task_manager_ref.queue_tasks(
            [{"agent": t.agent, "command": t.command, "context": t.context, "sujet": t.sujet} for t in tasks],
            chat_id=chat_id,
        ),
        _main_loop,
    )
    confirm = future.result(timeout=10)

    return TaskQueueResponse(
        status="queued",
        queued=len(tasks),
        message=confirm,
        task_ids=[t.id for t in tasks],
    )


@app.get("/tasks/status")
async def tasks_status(_=Depends(verify_secret)):
    """
    Paperclip interroge l'état de toutes les tâches en cours et récentes.
    Retourne le rapport formaté + un dict structuré pour parsing.
    """
    if not _task_manager_ref:
        return {"status": "unavailable", "report": "TaskManager non initialisé"}

    store = _task_manager_ref.store
    all_tasks = list(store._tasks.values())

    return {
        "status": "ok",
        "report": store.status_report(),
        "counts": {
            "running": sum(1 for t in all_tasks if t.status == "running"),
            "queued":  sum(1 for t in all_tasks if t.status == "queued"),
            "done":    sum(1 for t in all_tasks if t.status == "done"),
            "failed":  sum(1 for t in all_tasks if t.status in ("failed", "budget_exceeded")),
        },
        "tokens_used":  store.session_tokens_used(),
        "tokens_budget": 50_000,
    }


@app.post("/tasks/notify")
async def tasks_notify(request: Request, _=Depends(verify_secret)):
    """
    Paperclip envoie un message texte à JP via Telegram.
    Body: { "message": "...", "chat_id": 123 (optionnel) }
    """
    from core.telegram_notifier import notify
    from config import TELEGRAM_ALLOWED_USER_ID
    body = await request.json()
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message requis")
    await notify(msg)
    return {"status": "sent"}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard_index(_=Depends(verify_dashboard)):
    """Sert le dashboard HTML."""
    index = DASHBOARD_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Dashboard non trouvé"}


class DashboardCommand(BaseModel):
    text: str


@app.post("/dashboard/command")
async def dashboard_command(
    payload: DashboardCommand,
    _=Depends(verify_dashboard),
):
    """
    Exécute une commande depuis le dashboard et streame la réponse via SSE.
    Chaque événement SSE est un JSON: { type, text } ou { type: "budget", used, limit }
    """
    from core.brain import parse_intent, chat_respond
    from core.dispatcher import dispatch
    from core.guardrails import SessionBudget, BudgetExceededError, CallTimeoutError

    budget = SessionBudget()

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            # 1. Parse intent
            agent, command, context, reply = await parse_intent(
                payload.text, [], budget=budget
            )
            yield sse({"type": "budget", "used": budget.total, "limit": budget.limit})

            if reply:
                yield sse({"type": "reply", "text": reply})

            yield sse({"type": "progress", "text": f"{agent}.{command} en cours…"})

            # 2. Dispatch vers l'agent
            result = await asyncio.wait_for(
                dispatch(agent, command, context if context else None),
                timeout=float(os.environ.get("CLAUDE_CALL_TIMEOUT_S", "90")),
            )
            yield sse({"type": "budget", "used": budget.total, "limit": budget.limit})
            yield sse({"type": "result", "text": result or "✅ Opération terminée."})

        except BudgetExceededError as e:
            yield sse({"type": "error", "text": str(e)})
        except CallTimeoutError as e:
            yield sse({"type": "error", "text": str(e)})
        except asyncio.TimeoutError:
            yield sse({"type": "error", "text": "⏱️ Timeout — l'opération a pris trop de temps."})
        except Exception as e:
            log.error(f"dashboard.command error: {e}")
            yield sse({"type": "error", "text": f"❌ {e}"})

        yield sse({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
async def get_logs(
    n: int = 50,
    since: int = 0,
    _=Depends(verify_dashboard),
):
    """Snapshot JSON des N derniers logs (ou depuis since_id)."""
    from core.log_bus import bus
    entries = bus.tail(n)
    if since:
        entries = [e for e in entries if e.get("id", 0) > since]
    return {"logs": entries, "count": len(entries)}


@app.get("/logs/stream")
async def stream_logs(
    request: Request,
    since: int = 0,
    _=Depends(verify_dashboard),
):
    """SSE stream des logs en temps réel."""
    from core.log_bus import bus

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for entry in bus.stream(since_id=since):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(entry.to_dict(), ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/webhook/telegram")
async def telegram_notify(payload: dict, _=Depends(verify_secret)):
    """
    Envoie une notification Telegram depuis Paperclip.
    Usage: Paperclip peut notifier JP d'une action en attente d'approbation.
    """
    from config import TELEGRAM_ALLOWED_USER_ID, TELEGRAM_BOT_TOKEN
    import httpx

    message = payload.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message requis")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_ALLOWED_USER_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
        )
    return {"status": "sent", "telegram_response": r.status_code}
