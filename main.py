#!/usr/bin/env python3
"""
main.py — Point d'entrée unique du Welldone AI Agent Team.

Lance :
  1. Health check au démarrage (alerte Telegram si token cassé)
  2. Découverte des agents (auto-discovery)
  3. Bot Telegram (polling)
  4. Scheduler APScheduler (crons déclarés dans les agents)

Usage :
  python main.py              → démarrage normal
  python main.py --no-health  → skip le health check au boot
"""
import asyncio, sys, logging
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

    log.info("main: démarrage du bot Telegram (polling)...")

    # 6. Démarrer le bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("✅ Welldone AI Agent Team — En ligne")

    # Maintenir le process actif
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        log.info("main: arrêt demandé")
    finally:
        from core.scheduler import stop_scheduler
        stop_scheduler()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("main: arrêt propre")


if __name__ == "__main__":
    asyncio.run(main())
