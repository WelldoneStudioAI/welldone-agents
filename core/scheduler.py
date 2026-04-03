"""
core/scheduler.py — APScheduler (remplace GitHub Actions crons).

Les crons sont déclarés directement dans chaque agent via l'attribut `schedules`.
Ce module lit le dispatcher et enregistre tous les jobs automatiquement.
"""
import asyncio, logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config import TIMEZONE

log = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def _run_scheduled_job(agent_name: str, command: str, telegram_bot=None, chat_id: int | None = None):
    """Exécute un job schedulé et notifie Telegram si configuré."""
    from core.dispatcher import dispatch
    log.info(f"scheduler: running {agent_name}.{command}")
    try:
        result = await dispatch(agent_name, command)
        log.info(f"scheduler: {agent_name}.{command} done")
        # Notifier Telegram seulement si le résultat est non-vide (évite le spam "rien à faire")
        if telegram_bot and chat_id and result and result.strip():
            await telegram_bot.send_message(chat_id=chat_id, text=f"⏰ *Tâche auto:* /{agent_name} {command}\n\n{result}", parse_mode="Markdown")
    except Exception as e:
        msg = f"❌ Erreur tâche auto {agent_name}.{command}: {e}"
        log.error(msg)
        if telegram_bot and chat_id:
            await telegram_bot.send_message(chat_id=chat_id, text=msg)


def create_scheduler(telegram_bot=None, chat_id: int | None = None) -> AsyncIOScheduler:
    """
    Crée et configure le scheduler.
    Lit les schedules déclarés dans les agents via dispatcher.get_all_schedules().
    """
    from core.dispatcher import get_all_schedules

    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    schedules = get_all_schedules()
    for cron_expr, agent_name, command in schedules:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=TIMEZONE)
        _scheduler.add_job(
            _run_scheduled_job,
            trigger=trigger,
            args=[agent_name, command, telegram_bot, chat_id],
            id=f"{agent_name}_{command}",
            name=f"{agent_name}.{command}",
            replace_existing=True,
            misfire_grace_time=300,  # 5 min de délai toléré
        )
        log.info(f"⏰ Scheduled: {agent_name}.{command} @ cron({cron_expr})")

    log.info(f"Scheduler: {len(schedules)} job(s) configurés")
    return _scheduler


def start_scheduler(scheduler: AsyncIOScheduler):
    scheduler.start()
    log.info("✅ Scheduler démarré")


def stop_scheduler():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler arrêté")
