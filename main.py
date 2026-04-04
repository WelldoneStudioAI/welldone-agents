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
    from core.dispatcher import FAILED_AGENTS
    import os as _os
    _sha = _os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:7]

    log.info(f"[VALIDATION] commit={_sha}")
    log.info(f"[VALIDATION] agents_loaded={list(agents.keys())}")
    if FAILED_AGENTS:
        for mod, err in FAILED_AGENTS.items():
            log.error(f"[VALIDATION] agent_failed={mod} err={err[:120]}")
        log.error(f"[VALIDATION] status=DEPLOYED_NOT_VALIDATED — {len(FAILED_AGENTS)} agent(s) en échec")
    else:
        log.info(f"[VALIDATION] agents_failed=none")
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

        _WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

        class _RunPayload(BaseModel):
            sujet: str | None = None
            context: dict | None = None

        def _check_secret(authorization: str = Header(default="")):
            if not _WEBHOOK_SECRET:
                return  # Pas de secret configuré → accès ouvert (dev / Railway interne)
            token = authorization.replace("Bearer ", "").strip()
            if token != _WEBHOOK_SECRET:
                raise HTTPException(status_code=401, detail="Invalid token")

        # ── /livez — le process tourne (Railway healthcheck) ──────────────────
        @fastapi_app.get("/livez")
        async def _livez():
            return {"ok": True, "service": "welldone-agents"}

        # ── /healthz — preuve réelle de fonctionnement ─────────────────────────
        @fastapi_app.get("/healthz")
        async def _healthz():
            import os as _hzos
            from core.dispatcher import REGISTRY, FAILED_AGENTS, discover_agents
            if not REGISTRY:
                discover_agents()

            # Vars critiques
            _REQUIRED_VARS = [
                "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY",
                "FRAMER_API_KEY",
            ]
            missing_vars = [v for v in _REQUIRED_VARS if not _hzos.environ.get(v)]

            # Commit SHA depuis Railway (git non dispo dans le conteneur)
            sha = _hzos.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:7]

            agents_ok     = list(REGISTRY.keys())
            agents_failed = {k: v[:120] for k, v in FAILED_AGENTS.items()}
            healthy = not missing_vars and not agents_failed

            payload = {
                "ok":             healthy,
                "service":        "welldone-agents",
                "commit":         sha,
                "agents_loaded":  agents_ok,
                "agents_failed":  agents_failed,
                "missing_vars":   missing_vars,
                "timestamp":      _time.time(),
            }
            from fastapi.responses import JSONResponse
            return JSONResponse(payload, status_code=200 if healthy else 503)

        # ── /health — alias legacy ────────────────────────────────────────────
        @fastapi_app.get("/health")
        async def _health():
            from core.dispatcher import REGISTRY, discover_agents
            if not REGISTRY:
                discover_agents()
            return {"status": "ok", "agents": list(REGISTRY.keys()),
                    "timestamp": _time.time()}

        # ── Webhook formulaire site web ────────────────────────────────────────
        # Framer poste ici dès qu'un visiteur soumet le formulaire de contact.
        # Accepte JSON ET application/x-www-form-urlencoded (format Framer natif).
        # Notification Telegram immédiate — 0 délai, 0 email.
        # Variable Railway optionnelle: FRAMER_FORM_SECRET pour sécuriser.
        from fastapi import Request as _Request

        _FORM_SECRET = os.environ.get("FRAMER_FORM_SECRET", "")

        async def _send_lead_notification(data: dict) -> None:
            from core.telegram_notifier import notify
            name    = data.get("name") or data.get("Name") or data.get("prénom") or "Inconnu"
            email   = data.get("email") or data.get("Email") or data.get("courriel") or ""
            phone   = data.get("phone") or data.get("Phone") or data.get("téléphone") or ""
            message = data.get("message") or data.get("Message") or data.get("description") or ""
            source  = data.get("source") or data.get("page") or "awelldone.studio"
            lines = ["🔥 *NOUVEAU LEAD — Formulaire site*", f"👤 *{name}*"]
            if email:   lines.append(f"📧 {email}")
            if phone:   lines.append(f"📞 {phone}")
            if message: lines.append(f"\n💬 _{message[:300]}_")
            lines.append(f"\n🌐 {source}")
            await notify("\n".join(lines))
            log.info(f"webhook/form: lead reçu — {name} <{email}>")

        @fastapi_app.post("/webhook/form")
        async def _form_webhook(request: _Request,
                                authorization: str = Header(default="")):
            # Vérification token si configuré
            if _FORM_SECRET:
                token = authorization.replace("Bearer ", "").strip()
                if token != _FORM_SECRET:
                    raise HTTPException(status_code=401, detail="Invalid token")

            # Accepter JSON ou form-encoded (Framer envoie les deux selon la config)
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                data = await request.json()
            else:
                form = await request.form()
                data = dict(form)

            await _send_lead_notification(data)
            return {"status": "ok", "notified": True}

        @fastapi_app.get("/agents")
        async def _list_agents(authorization: str = Header(default="")):
            _check_secret(authorization)
            from core.dispatcher import REGISTRY, discover_agents
            if not REGISTRY:
                discover_agents()
            return {
                name: {"description": a.description, "commands": list(a.commands.keys())}
                for name, a in REGISTRY.items()
            }

        @fastapi_app.post("/run/{agent_name}/{command}")
        async def _run_agent(agent_name: str, command: str, payload: _RunPayload,
                             authorization: str = Header(default="")):
            """Déclenche un agent/commande directement — utilisé par le cron Railway et les webhooks."""
            _check_secret(authorization)
            import asyncio as _aio
            try:
                from core.dispatcher import REGISTRY, discover_agents
                if not REGISTRY:
                    discover_agents()
                if agent_name not in REGISTRY:
                    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' inconnu")
                agent_ctx = dict(payload.context or {})
                if payload.sujet:
                    agent_ctx["sujet"] = payload.sujet
                result = await _aio.wait_for(
                    REGISTRY[agent_name].run_command(command, agent_ctx or None),
                    timeout=300,
                )
                return {"status": "success", "result": str(result)}
            except _aio.TimeoutError:
                raise HTTPException(status_code=504, detail="Timeout 5 min dépassé")
            except Exception as exc:
                log.error(f"/run/{agent_name}/{command} error: {exc}")
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
        log.info(f"main: API HTTP → http://0.0.0.0:{port}")
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
