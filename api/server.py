"""
api/server.py — Serveur HTTP FastAPI pour Paperclip HTTP Adapter.

Expose chaque agent comme endpoint REST que Paperclip peut appeler.
Tourne en parallèle du bot Telegram sur le port $PORT (Railway) ou 8080.

Format Paperclip HTTP Adapter:
  POST /agents/{name}/{command}
  Body: { "runId": "...", "agentId": "...", "taskId": "...", "context": {...} }
  Returns: { "status": "success"|"error", "result": "...", "usage": {...} }
"""
import os, time, logging
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any

log = logging.getLogger(__name__)

app = FastAPI(
    title="Welldone AI Agents",
    description="HTTP Adapter pour Paperclip — Welldone Studio",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Secret pour valider les requêtes Paperclip
WEBHOOK_SECRET = os.environ.get("PAPERCLIP_WEBHOOK_SECRET", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_secret(authorization: str = Header(default="")):
    """Vérifie le Bearer token si PAPERCLIP_WEBHOOK_SECRET est configuré."""
    if not WEBHOOK_SECRET:
        return  # Pas de secret configuré → open (dev local)
    token = authorization.replace("Bearer ", "").strip()
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")


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
