"""
core/telegram_notifier.py — Module partagé pour envoyer des notifications Telegram.

Stocke une référence au bot PTB et au chat_id de JP.
Initialisé une seule fois au démarrage depuis main.py → build_app().

Usage :
    from core.telegram_notifier import set_bot, notify
    await notify("Message important")
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_bot: Any = None       # telegram.Bot instance
_chat_id: int = 0      # chat_id de JP (TELEGRAM_ALLOWED_USER_ID)


def set_bot(bot, chat_id: int) -> None:
    """Enregistre le bot PTB et le chat_id. Appelé depuis build_app()."""
    global _bot, _chat_id
    _bot = bot
    _chat_id = chat_id
    log.info(f"telegram_notifier: bot enregistré — chat_id={chat_id}")


async def notify(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Envoie un message Telegram à JP.

    Returns:
        True si envoyé, False si bot non initialisé ou erreur.
    """
    if _bot is None or _chat_id == 0:
        log.warning("telegram_notifier: bot non initialisé — notification ignorée")
        return False
    try:
        # Tronquer si > 4096 chars (limite Telegram)
        msg = text[:4096] if len(text) > 4096 else text
        await _bot.send_message(chat_id=_chat_id, text=msg, parse_mode=parse_mode)
        log.info(f"telegram_notifier: notification envoyée ({len(msg)} chars)")
        return True
    except Exception as e:
        log.error(f"telegram_notifier: erreur envoi: {e}")
        return False
