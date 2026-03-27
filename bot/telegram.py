"""
bot/telegram.py — Interface Telegram (thin layer).

RÈGLE : Ce fichier ne contient AUCUNE logique métier.
Il parse les commandes et route vers core/dispatcher.py ou core/brain.py.
"""
import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from core.dispatcher import dispatch, help_text, discover_agents
from core.brain import parse_intent, chat_respond
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID

log = logging.getLogger(__name__)

# Historique de conversation par user_id (rolling window)
_conversations: dict[int, list[dict]] = {}
MAX_HISTORY = 20


def _get_history(user_id: int) -> list[dict]:
    return _conversations.setdefault(user_id, [])


def _add_to_history(user_id: int, role: str, content: str):
    history = _get_history(user_id)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        _conversations[user_id] = history[-MAX_HISTORY:]


def _is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == TELEGRAM_ALLOWED_USER_ID


async def _send(update: Update, text: str):
    """Envoie un message en gérant les limites de taille Telegram (4096 chars)."""
    if len(text) <= 4096:
        await update.effective_message.reply_text(text, parse_mode="Markdown")
        return
    # Découper en chunks
    for i in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[i:i+4000], parse_mode="Markdown")


# ── Commandes slash ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    text = await help_text()
    await _send(update, text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    _conversations[update.effective_user.id] = []
    await update.message.reply_text("🔄 Conversation réinitialisée.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    await _send(update, await help_text())


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    await update.message.reply_text("⏳ Vérification des services...")
    # Import ici pour éviter les imports circulaires
    import health as h
    results = h.run_checks()
    report  = h.format_report(results)
    await update.message.reply_text(report)


async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler générique pour /gmail, /calendar, /notion, /analytics, /zoho, /veille.
    Extrait l'agent depuis la commande et la sous-commande depuis les args.

    Ex: /gmail read → dispatch("gmail", "read")
        /analytics rapport --days 7 → dispatch("analytics", "rapport", {"days": 7})
    """
    if not _is_allowed(update): return

    cmd_text   = update.message.text or ""
    parts      = cmd_text.lstrip("/").split()
    agent_name = parts[0] if parts else ""
    command    = parts[1] if len(parts) > 1 else "help"

    # Extraire les paramètres simples --key value
    ctx = {}
    i   = 2
    while i < len(parts):
        if parts[i].startswith("--") and i + 1 < len(parts):
            key = parts[i][2:]
            ctx[key] = parts[i + 1]
            i += 2
        else:
            i += 1

    if command == "help":
        agent_obj = discover_agents().get(agent_name)
        if agent_obj:
            await _send(update, await agent_obj.help())
        else:
            await update.message.reply_text(f"❌ Agent `{agent_name}` introuvable")
        return

    await update.message.reply_text(f"⏳ /{agent_name} {command}...")
    result = await dispatch(agent_name, command, ctx or None)
    await _send(update, result)


# ── Messages naturels ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return

    user_id = update.effective_user.id
    text    = update.message.text or ""

    _add_to_history(user_id, "user", text)

    # Afficher indicateur de frappe
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Parse l'intention avec Claude
    history = _get_history(user_id)
    agent_name, command, ctx, reply = await parse_intent(text, history[:-1])

    # Si Claude a un message de confirmation à afficher
    if reply:
        await update.message.reply_text(reply)

    # Exécuter l'action
    if agent_name == "chat":
        result = await chat_respond(text, history[:-1])
    else:
        result = await dispatch(agent_name, command, ctx)

    _add_to_history(user_id, "assistant", result)
    await _send(update, result)


# ── Setup de l'application ─────────────────────────────────────────────────────

def build_app() -> Application:
    """Construit et retourne l'application Telegram configurée."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commandes fixes
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("health", cmd_health))

    # Commandes agents (enregistrées dynamiquement après découverte)
    agents = discover_agents()
    for agent_name in agents:
        app.add_handler(CommandHandler(agent_name, cmd_agent))
        log.info(f"Telegram: handler /{agent_name} enregistré")

    # Messages naturels
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


async def set_bot_commands(app: Application):
    """Met à jour la liste des commandes visibles dans Telegram."""
    agents  = discover_agents()
    commands = [
        BotCommand("start",  "Aide et liste des agents"),
        BotCommand("help",   "Aide complète"),
        BotCommand("health", "Vérifier tous les services"),
        BotCommand("reset",  "Réinitialiser la conversation"),
    ] + [
        BotCommand(name, agent.description[:50])
        for name, agent in agents.items()
    ]
    await app.bot.set_my_commands(commands)
    log.info(f"Telegram: {len(commands)} commandes configurées")
