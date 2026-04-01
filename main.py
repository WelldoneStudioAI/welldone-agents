#!/usr/bin/env python3
"""
main.py — Point d'entrée unique du Welldone AI Agent Team.

Lance :
  1. Health check au démarrage (alerte Telegram si token cassé)
  2. Découverte des agents (auto-discovery)
  3. Bot Telegram (polling)
  4. Scheduler APScheduler (crons déclarés dans les agents)
  5. API FastAPI (Paperclip HTTP Adapter) sur $PORT

Usage :
  python main.py              → démarrage normal
  python main.py --no-health  → skip le health check au boot
"""
import asyncio, sys, os, logging

# S'assurer que le répertoire du script est dans sys.path (fix nixpacks/railway up)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Log bus en premier (avant setup_logging) pour capturer tous les logs ──────
from core.log_bus import install_log_bus
install_log_bus()

from core.log import setup_logging, get_logger

setup_logging()
log = get_logger("main")


async def startup_health_check(bot) -> bool:
    """Vérifie tous les tokens au démarrage et alerte si problème."""
    import health as h
    from config import TELEGRAM_ALLOWED_USER_ID

    log.info("main: startup health check...")
    results = h.run_checks()
    errors  = [r for r in results if r["status"] == "error"]

    if errors:
        report = h.format_report(results)
        try:
            await bot.send_message(
                chat_id=TELEGRAM_ALLOWED_USER_ID,
                text=f"⚠️ *Démarrage — Services en erreur :*\n\n{report}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        log.warning(f"main: {len(errors)} service(s) en erreur au démarrage")
    else:
        log.info("main: tous les services sont opérationnels")

    return len(errors) == 0


async def main():
    from bot.telegram import build_app, set_bot_commands
    from core.dispatcher import discover_agents
    from core.scheduler import create_scheduler, start_scheduler
    from config import TELEGRAM_ALLOWED_USER_ID

    skip_health = "--no-health" in sys.argv

    log.info("╔══════════════════════════════════════╗")
    log.info("║  WELLDONE AI AGENT TEAM — Démarrage  ║")
    log.info("╚══════════════════════════════════════╝")

    # 1. Découverte des agents
    agents = discover_agents()
    log.info(f"main: {len(agents)} agents chargés: {list(agents.keys())}")

    # 2. Build Telegram app
    app = build_app()

    # 3. Health check
    if not skip_health:
        await startup_health_check(app.bot)

    # 4. Mettre à jour les commandes Telegram
    await set_bot_commands(app)

    # 5. Créer le scheduler (notifie sur Telegram)
    scheduler = create_scheduler(
        telegram_bot=app.bot,
        chat_id=TELEGRAM_ALLOWED_USER_ID,
    )
    start_scheduler(scheduler)

    # 6. API FastAPI inline — construit directement ici, zéro import externe
    import os, threading, time as _time, json as _json
    port = int(os.environ.get("PORT", 8080))
    try:
        import uvicorn
        from fastapi import FastAPI, HTTPException, Header, Request
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel
        from typing import Any

        fastapi_app = FastAPI(title="Welldone AI Agents", version="2.1.0")
        fastapi_app.add_middleware(CORSMiddleware, allow_origins=["*"],
                                   allow_methods=["*"], allow_headers=["*"])

        _WEBHOOK_SECRET = os.environ.get("PAPERCLIP_WEBHOOK_SECRET", "")
        _SLUG_MAP = {
            # ── CEO — lit la queue Paperclip et dispatche ──────────────────────
            "ceo":            ("ceo",       "dispatch"),
            "ceo-status":     ("ceo",       "status"),
            # ── Agents spécialisés ────────────────────────────────────────────
            "chef-marketing": ("blog",      "rédiger"),
            "chef-seo":       ("analytics", "rapport"),
            "chef-design":    ("framer",    "liste"),
            "chef-email":     ("email",     "trier"),
            "veille":         ("veille",    "run"),
            "blog-rediger":   ("blog",      "rédiger"),
            "analytics":      ("analytics", "rapport"),
            "framer-rediger": ("framer",    "rédiger"),
        }

        class _NativeCtx(BaseModel):
            taskId: str | None = None
            wakeReason: str | None = "task_assigned"
            commentId: str | None = None

        class _NativePayload(BaseModel):
            runId: str | None = None
            agentId: str | None = None
            companyId: str | None = None
            context: _NativeCtx | None = None

        def _check_secret(authorization: str = Header(default="")):
            if not _WEBHOOK_SECRET:
                return
            token = authorization.replace("Bearer ", "").strip()
            if token != _WEBHOOK_SECRET:
                raise HTTPException(status_code=401, detail="Invalid token")

        @fastapi_app.get("/health")
        async def _health():
            from core.dispatcher import REGISTRY, discover_agents
            if not REGISTRY:
                discover_agents()
            return {"status": "ok", "agents": list(REGISTRY.keys()),
                    "timestamp": _time.time()}

        @fastapi_app.get("/paperclip/agents")
        async def _paperclip_agents():
            return {"slugs": list(_SLUG_MAP.keys())}

        @fastapi_app.post("/paperclip/{slug}")
        async def _paperclip_run(slug: str, payload: _NativePayload,
                                 authorization: str = Header(default="")):
            _check_secret(authorization)
            if slug not in _SLUG_MAP:
                raise HTTPException(status_code=404,
                                    detail=f"Slug '{slug}' inconnu. Disponibles: {list(_SLUG_MAP.keys())}")
            agent_name, command = _SLUG_MAP[slug]
            ctx = payload.context or _NativeCtx()
            task_id = ctx.taskId or (payload.runId or "paperclip")
            _budgets = {"chef-marketing": 15000, "chef-seo": 8000,
                        "veille": 10000, "chef-design": 2000}
            budget_tokens = _budgets.get(slug, 5000)

            import asyncio as _aio
            try:
                from core.dispatcher import REGISTRY, discover_agents
                from core.guardrails import SessionBudget
                if not REGISTRY:
                    discover_agents()
                if agent_name not in REGISTRY:
                    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' non trouvé")
                agent = REGISTRY[agent_name]
                budget = SessionBudget(limit=budget_tokens)
                result = await _aio.wait_for(
                    agent.run_command(command, {"task_id": task_id,
                                               "wake_reason": ctx.wakeReason or "task_assigned"}),
                    timeout=300,
                )
                return {"status": "success", "result": str(result),
                        "usage": {"tokens_used": budget.total,
                                  "max_tokens": budget_tokens}}
            except _aio.TimeoutError:
                raise HTTPException(status_code=504, detail="Timeout 5 min dépassé")
            except Exception as exc:
                log.error(f"paperclip/{slug} error: {exc}")
                raise HTTPException(status_code=500, detail=str(exc))

        def run_api():
            import asyncio as _aio
            from core.log_bus import bus as _bus
            loop = _aio.new_event_loop()
            _aio.set_event_loop(loop)
            _bus.set_loop(loop)
            uvicorn.run(fastapi_app, host="0.0.0.0", port=port,
                        log_level="warning", access_log=False)

        api_thread = threading.Thread(target=run_api, daemon=True, name="uvicorn-api")
        api_thread.start()
        log.info(f"main: API Paperclip inline → http://0.0.0.0:{port}")
    except Exception as e:
        import traceback
        log.error(f"main: API inline failed ({e})\n{traceback.format_exc()}")
        # Fallback stdlib — Railway doit toujours avoir quelque chose sur $PORT
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class _FallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = _json.dumps({"status": "ok", "mode": "telegram-only"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = _json.dumps({"error": "api_unavailable"}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        _srv = HTTPServer(("0.0.0.0", port), _FallbackHandler)
        threading.Thread(target=_srv.serve_forever, daemon=True,
                         name="fallback-http").start()
        log.warning(f"main: fallback stdlib HTTP sur port {port}")

    # 7. Bot Telegram — démarrage propre dans l'event loop principal
    log.info("main: démarrage du bot Telegram...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False)
        log.info("✅ Welldone AI Agent Team — En ligne")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("main: arrêt demandé")
        finally:
            from core.scheduler import stop_scheduler
            stop_scheduler()
            await app.updater.stop()
            await app.stop()
            log.info("main: arrêt propre")


if __name__ == "__main__":
    asyncio.run(main())
