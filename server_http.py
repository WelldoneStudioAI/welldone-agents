"""
api/server.py — Serveur HTTP FastAPI pour Paperclip HTTP Adapter + Dashboard web.

Expose chaque agent comme endpoint REST que Paperclip peut appeler.
Tourne en parallèle du bot Telegram sur le port $PORT (Railway) ou 8080.
# 2026-04-05 — ajout endpoint /archi-news-links (cache 5min)

Format Paperclip HTTP Adapter (format officiel Paperclip):
  POST /paperclip/{slug}
  Body: { "runId": "...", "agentId": "...", "companyId": "...",
          "context": { "taskId": "...", "wakeReason": "...", "commentId": "..." } }
  Returns: { "status": "success"|"error", "result": "...", "usage": {...} }

  Mapping slug → agent/command (configurable via PAPERCLIP_AGENTS env):
    chef-marketing   → blog.rédiger
    chef-seo         → analytics.rapport
    chef-design      → framer.liste
    chef-email       → email.trier
    veille           → veille.run

Format legacy (toujours supporté):
  POST /agents/{name}/{command}
  Body: { "runId": "...", "agentId": "...", "taskId": "...", "context": {...} }

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

# ── Mapping Paperclip slug → agent/command ────────────────────────────────────
# Format JSON: '{"chef-marketing": ["blog", "rédiger"], "chef-seo": ["analytics", "rapport"]}'
# Peut être surchargé via PAPERCLIP_AGENTS en variable Railway
_DEFAULT_SLUG_MAP = {
    "chef-marketing":  ("blog",      "rédiger"),
    "chef-seo":        ("analytics", "rapport"),
    "chef-design":     ("framer",    "liste"),
    "chef-email":      ("email",     "trier"),
    "veille":          ("veille",    "run"),
    "blog-rediger":    ("blog",      "rédiger"),
    "analytics":       ("analytics", "rapport"),
    "framer-rediger":  ("framer",    "rédiger"),
}

def _get_slug_map() -> dict:
    raw = os.environ.get("PAPERCLIP_AGENTS", "")
    if raw:
        try:
            overrides = json.loads(raw)
            return {**_DEFAULT_SLUG_MAP, **{k: tuple(v) for k, v in overrides.items()}}
        except Exception:
            pass
    return _DEFAULT_SLUG_MAP


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
    """Format legacy (compatible avec l'ancienne version)."""
    runId: str | None = None
    agentId: str | None = None
    taskId: str | None = None
    wakeReason: str | None = "task_assigned"
    context: dict[str, Any] | None = None


class PaperclipNativeContext(BaseModel):
    """Contexte imbriqué tel qu'envoyé par Paperclip HTTP Adapter officiel."""
    taskId: str | None = None
    wakeReason: str | None = "task_assigned"
    commentId: str | None = None


class PaperclipNativePayload(BaseModel):
    """Format officiel Paperclip HTTP Adapter v2.
    POST body: { runId, agentId, companyId, context: { taskId, wakeReason, commentId } }
    """
    runId: str | None = None
    agentId: str | None = None
    companyId: str | None = None
    context: PaperclipNativeContext | None = None


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


@app.post("/paperclip/{slug}", response_model=PaperclipResponse)
async def paperclip_run(
    slug: str,
    payload: PaperclipNativePayload,
    _=Depends(verify_secret),
):
    """
    Endpoint principal pour Paperclip HTTP Adapter (format officiel v2).
    Paperclip POST → /paperclip/{slug}
    Le slug est configuré dans l'UI Paperclip et mappe vers un agent/command précis.

    Budgets par agent (guardrails tokens) :
      - chef-marketing / blog.rédiger   : 15 000 tokens
      - chef-seo / analytics.rapport    : 8 000 tokens
      - chef-design / framer.liste      : 2 000 tokens
      - chef-email / email.trier        : 5 000 tokens
      - veille / veille.run             : 10 000 tokens
    """
    from core.dispatcher import dispatch
    from core.guardrails import SessionBudget, BudgetExceededError, CallTimeoutError

    slug_map = _get_slug_map()
    if slug not in slug_map:
        raise HTTPException(
            status_code=404,
            detail=f"Slug '{slug}' inconnu. Slugs disponibles: {list(slug_map.keys())}"
        )

    agent_name, command = slug_map[slug]
    ctx = payload.context
    wake_reason = ctx.wakeReason if ctx else "heartbeat"

    start = time.time()
    log.info(
        f"paperclip: {slug} → {agent_name}.{command} | "
        f"runId={payload.runId} | wakeReason={wake_reason}"
    )

    # Budget guardrail par agent (tokens max)
    SLUG_BUDGETS = {
        "chef-marketing":  15_000,
        "blog-rediger":    15_000,
        "framer-rediger":  15_000,
        "chef-seo":        8_000,
        "analytics":       8_000,
        "chef-email":      5_000,
        "veille":          10_000,
        "chef-design":     2_000,
    }
    max_tokens = SLUG_BUDGETS.get(slug, 10_000)
    budget = SessionBudget(max_tokens=max_tokens)

    try:
        result = await asyncio.wait_for(
            dispatch(agent_name, command, None),
            timeout=float(os.environ.get("CLAUDE_CALL_TIMEOUT_S", "180")),
        )
        elapsed = round(time.time() - start, 2)
        log.info(f"paperclip: {slug} → succès en {elapsed}s | tokens≤{max_tokens}")
        return PaperclipResponse(
            status="success",
            result=result or "✅ Terminé.",
            usage={
                "duration_seconds": elapsed,
                "agent": agent_name,
                "command": command,
                "token_budget": max_tokens,
                "wake_reason": wake_reason,
                "run_id": payload.runId,
            },
        )
    except BudgetExceededError as e:
        elapsed = round(time.time() - start, 2)
        log.warning(f"paperclip: {slug} → budget dépassé: {e}")
        return PaperclipResponse(
            status="error",
            result=f"🛑 Budget tokens dépassé ({max_tokens} max): {e}",
            usage={"duration_seconds": elapsed, "agent": agent_name, "command": command},
        )
    except asyncio.TimeoutError:
        elapsed = round(time.time() - start, 2)
        log.error(f"paperclip: {slug} → timeout après {elapsed}s")
        return PaperclipResponse(
            status="error",
            result=f"⏱️ Timeout — {agent_name}.{command} a pris plus de {elapsed}s",
            usage={"duration_seconds": elapsed, "agent": agent_name, "command": command},
        )
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        log.error(f"paperclip: {slug} → erreur: {e}")
        return PaperclipResponse(
            status="error",
            result=f"❌ Erreur {agent_name}.{command}: {e}",
            usage={"duration_seconds": elapsed, "agent": agent_name, "command": command},
        )


@app.get("/paperclip/agents")
async def paperclip_list_agents(_=Depends(verify_secret)):
    """Liste les slugs Paperclip disponibles avec leur mapping agent/command."""
    slug_map = _get_slug_map()
    SLUG_BUDGETS = {
        "chef-marketing":  15_000,
        "blog-rediger":    15_000,
        "framer-rediger":  15_000,
        "chef-seo":        8_000,
        "analytics":       8_000,
        "chef-email":      5_000,
        "veille":          10_000,
        "chef-design":     2_000,
    }
    return {
        slug: {
            "agent": agent,
            "command": cmd,
            "token_budget": SLUG_BUDGETS.get(slug, 10_000),
            "url": f"/paperclip/{slug}",
        }
        for slug, (agent, cmd) in slug_map.items()
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


# ── Archi News Links — redirect dynamique ─────────────────────────────────────

_archi_links_cache: dict = {}
_archi_links_ts: float = 0.0
_ARCHI_LINKS_TTL = 300  # 5 minutes

@app.get("/archi-news-links")
async def archi_news_links():
    """
    Retourne { slug: external_url } pour tous les articles Architecture-Blog.
    Utilisé par le script custom Framer pour rediriger /archi/archi-news/:slug
    vers la source externe (ArchDaily, Azure, Archello, etc.).
    Mis en cache 5 minutes. Aucune auth requise (données publiques).
    """
    global _archi_links_cache, _archi_links_ts

    now = time.time()
    if _archi_links_cache and (now - _archi_links_ts) < _ARCHI_LINKS_TTL:
        return _archi_links_cache

    import subprocess, json as _json, pathlib
    helper = pathlib.Path(__file__).parent / "framer_helper.js"
    node   = "/usr/local/bin/node"
    env    = {**os.environ, "FRAMER_COLLECTION_ID": "QN2U_mvQ4"}

    try:
        result = subprocess.run(
            [node, str(helper), "links"],
            capture_output=True, text=True, env=env,
            cwd=str(helper.parent), timeout=30,
        )
        data = _json.loads(result.stdout.strip())
        if data.get("ok") and "links" in data:
            _archi_links_cache = data["links"]
            _archi_links_ts    = now
            return _archi_links_cache
    except Exception as e:
        log.error(f"archi-news-links: {e}")

    # Fallback: cache précédent ou dict vide
    return _archi_links_cache or {}


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
