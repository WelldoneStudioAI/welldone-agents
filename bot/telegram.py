"""
bot/telegram.py — Interface Telegram (thin layer).

RÈGLE : Ce fichier ne contient AUCUNE logique métier.
Il parse les commandes et route vers core/dispatcher.py ou core/brain.py.
"""
import logging, io
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from core.dispatcher import dispatch, help_text, discover_agents
from core.brain import parse_intent, chat_respond
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
from agents.qbo import (
    store_pending, get_pending, clear_pending,
    execute_create, execute_send_direct,
    _format_preview, _next_invoice_number, _get_qbo_tax_code,
)

from agents.qbo import QBOAgent as _QBOAgent

# Signaux retournés par qbo.create()
_QBO_NEEDS_CONFIRMATION = _QBOAgent.NEEDS_CONFIRMATION
_QBO_NEEDS_SERVICE      = _QBOAgent.NEEDS_SERVICE

log = logging.getLogger(__name__)

# Historique de conversation par user_id (rolling window)
_conversations: dict[int, list[dict]] = {}
MAX_HISTORY = 20

# ── TaskManager (initialisé dans build_app) ───────────────────────────────────
_task_manager = None


def get_task_manager():
    return _task_manager

# ── Keyboard services ─────────────────────────────────────────────────────────
SERVICE_MAP = {
    "svc_corporate":    "Photographie corporative",
    "svc_commercial":   "Photographie commerciale",
    "svc_digital":      "Service digital",
    "svc_consultation": "Consultation stratégique",
}

SERVICE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📸 Photo corporative",  callback_data="svc_corporate"),
        InlineKeyboardButton("🏪 Photo commerciale",  callback_data="svc_commercial"),
    ],
    [
        InlineKeyboardButton("💻 Service digital",    callback_data="svc_digital"),
        InlineKeyboardButton("🧠 Consultation",       callback_data="svc_consultation"),
    ],
])


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
    """Envoie un message en Markdown, en gérant les limites de taille Telegram (4096 chars)."""
    if len(text) <= 4096:
        await update.effective_message.reply_text(text, parse_mode="Markdown")
        return
    for i in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[i:i+4000], parse_mode="Markdown")


# Alias pour la lisibilité
_send_md = _send


# ── Preview QBO ───────────────────────────────────────────────────────────────

async def _show_qbo_preview(update: Update, user_id: int):
    """Affiche la preview de facturation avec les boutons d'action."""
    data = get_pending(user_id)
    if not data:
        await update.message.reply_text("❌ Erreur interne : données pending introuvables.")
        return

    preview_text = _format_preview(data)

    tax_warning = ""
    if data.get("_tax_warning"):
        tax_warning = "\n\n⚠️ _Aucun TaxCode TPS/TVQ trouvé dans QBO — facture créée sans taxe._"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Créer dans QuickBooks",  callback_data=f"qbo_draft_{user_id}"),
            InlineKeyboardButton("📤 Envoyer directement",   callback_data=f"qbo_send_{user_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Modifier client",  callback_data=f"qbo_edit_client_{user_id}"),
            InlineKeyboardButton("✏️ Modifier montant", callback_data=f"qbo_edit_amount_{user_id}"),
        ],
        [
            InlineKeyboardButton("❌ Annuler", callback_data=f"qbo_cancel_{user_id}"),
        ],
    ])

    await update.message.reply_text(
        preview_text + tax_warning,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Commandes slash ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    text = await help_text()
    await _send_md(update, text)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    _conversations[update.effective_user.id] = []
    await update.message.reply_text("🔄 Conversation réinitialisée.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    await _send_md(update, await help_text())


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")
    await update.message.reply_text(f"🟢 En ligne — {now}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le status des tâches parallèles en cours."""
    if not _is_allowed(update): return
    tm = get_task_manager()
    if tm is None:
        await update.message.reply_text("TaskManager non initialisé.")
        return
    report = tm.get_status_report()
    await _send_md(update, report)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return
    await update.message.reply_text("⏳ Vérification des services...")
    import health as h
    results = h.run_checks()
    report  = h.format_report(results)
    await update.message.reply_text(report)


async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler générique pour /gmail, /calendar, /notion, /analytics, /qbo, /veille.
    Ex: /gmail read → dispatch("gmail", "read")
        /analytics rapport --days 7 → dispatch("analytics", "rapport", {"days": 7})
    """
    if not _is_allowed(update): return

    cmd_text   = update.message.text or ""
    parts      = cmd_text.lstrip("/").split()
    agent_name = parts[0] if parts else ""
    command    = parts[1] if len(parts) > 1 else "help"

    ctx        = {}
    positional = []
    i          = 2
    while i < len(parts):
        if parts[i].startswith("--") and i + 1 < len(parts):
            # --key value
            key = parts[i][2:]
            ctx[key] = parts[i + 1]
            i += 2
        elif parts[i].endswith(":") and i + 1 < len(parts):
            # "KEY: value"  (ex: "ID: e9vXkIRUA")
            key = parts[i][:-1].lower()
            ctx[key] = parts[i + 1]
            i += 2
        else:
            positional.append(parts[i])
            i += 1
    # Premier arg positionnel → "id" si pas déjà fourni
    # Permet: /framer supprimer e9vXkIRUA   (sans --id)
    if positional and "id" not in ctx:
        ctx["id"] = positional[0]

    if command == "help":
        agent_obj = discover_agents().get(agent_name)
        if agent_obj:
            await _send_md(update, await agent_obj.help())
        else:
            await update.message.reply_text(f"❌ Agent `{agent_name}` introuvable")
        return

    await update.message.reply_text(f"⏳ /{agent_name} {command}...")
    result = await dispatch(agent_name, command, ctx or None)
    await _send_md(update, result)


# ── Messages naturels ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update): return

    user_id = update.effective_user.id
    text    = update.message.text or ""

    # ── Gestion des éditions en attente (Modifier client / Modifier montant) ──
    awaiting = context.user_data.get("awaiting_edit")
    if awaiting:
        pending = get_pending(user_id)
        if pending:
            if awaiting == "client":
                # Rechercher le nouveau client dans QBO
                from agents.qbo import _find_customer
                new_customer = _find_customer(text.strip())
                if new_customer:
                    pending["customer_id"]   = new_customer["Id"]
                    pending["customer_name"] = new_customer.get("DisplayName", text.strip())
                    pending["email"]         = new_customer.get("PrimaryEmailAddr", {}).get("Address", "—")
                    # Recalculer le numéro si nécessaire (garder le même)
                    store_pending(user_id, pending)
                    context.user_data.pop("awaiting_edit", None)
                    await update.message.reply_text(f"✅ Client mis à jour : {pending['customer_name']}")
                    await _show_qbo_preview(update, user_id)
                else:
                    await update.message.reply_text(
                        f"❓ Client *{text.strip()}* introuvable dans QBO.\n"
                        f"Tape son email pour le créer ou recommence.",
                        parse_mode="Markdown",
                    )
                return

            elif awaiting == "amount":
                try:
                    new_amount = float(text.strip().replace(",", ".").replace("$", "").replace(" ", ""))
                    pending["amount"] = new_amount
                    pending["tps"]    = round(new_amount * 0.05, 2)
                    pending["tvq"]    = round(new_amount * 0.09975, 2)
                    pending["total"]  = round(new_amount + pending["tps"] + pending["tvq"], 2)
                    store_pending(user_id, pending)
                    context.user_data.pop("awaiting_edit", None)
                    await update.message.reply_text(f"✅ Montant mis à jour : ${new_amount:,.2f}")
                    await _show_qbo_preview(update, user_id)
                except ValueError:
                    await update.message.reply_text("❌ Montant invalide. Ex: 2500 ou 1 250,00")
                return

    _add_to_history(user_id, "user", text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    history = _get_history(user_id)
    intent_result = await parse_intent(text, history[:-1])

    # ── Format multi-tâches ───────────────────────────────────────────────────
    if isinstance(intent_result, dict) and "tasks" in intent_result:
        tasks  = intent_result["tasks"]
        reply  = intent_result.get("reply", f"Lancement de {len(tasks)} taches en parallele...")
        tm = get_task_manager()
        if tm is None:
            await update.message.reply_text("TaskManager non disponible — relance le bot.")
            return
        confirm = await tm.queue_tasks(tasks, chat_id=user_id)
        _add_to_history(user_id, "assistant", reply)
        await update.message.reply_text(confirm)
        return

    # ── Format tâche unique (défaut) ─────────────────────────────────────────
    agent_name, command, ctx, reply = intent_result

    if reply:
        await update.message.reply_text(reply)

    # Messages de progression pour les opérations longues (envoyés immédiatement)
    _PROGRESS = {
        ("blog",    "rédiger"):   "🚀 Pipeline blog lancé en arrière-plan — rédaction + images + scoring qualité.\n_Je te notifie quand c'est prêt (2-4 min)._",
        ("framer",  "rédiger"):   "✍️ Je génère l'article avec Claude puis je le pousse dans Framer...\n_(1 à 2 minutes — je te confirme quand c'est fait)_",
        ("framer",  "liste"):     "📋 Connexion à Framer CMS en cours...",
        ("framer",  "supprimer"): "🗑️ Suppression en cours...",
        ("voyage",  None):        "✈️ Recherche de vols en cours... _(jusqu'à 60s)_",
        ("veille",  None):        "🔍 Veille en cours...",
        ("analytics", None):      "📊 Récupération des données Analytics...",
    }
    progress_msg = _PROGRESS.get((agent_name, command)) or _PROGRESS.get((agent_name, None))
    if progress_msg and agent_name != "chat":
        await update.message.reply_text(progress_msg, parse_mode="Markdown")

    # Agents lents — maintenir l'indicateur de frappe (···)
    SLOW_AGENTS = {"voyage", "framer", "veille", "analytics"}
    if agent_name in SLOW_AGENTS:
        import asyncio as _asyncio

        async def _keep_typing():
            for _ in range(12):  # max 60 secondes (5s x 12)
                await _asyncio.sleep(5)
                try:
                    await context.bot.send_chat_action(
                        chat_id=update.effective_chat.id, action="typing"
                    )
                except Exception:
                    break

        typing_task = _asyncio.create_task(_keep_typing())
    else:
        typing_task = None

    # Cas spécial QBO create → flux preview
    if agent_name == "qbo" and command == "create":
        # Injecter user_id dans le context pour que l'agent puisse stocker le pending
        ctx["_user_id"] = user_id
        result = await dispatch(agent_name, command, ctx)

        if result == _QBO_NEEDS_SERVICE:
            # Afficher les pills de sélection de service
            pending = get_pending(user_id)
            await update.message.reply_text(
                f"📋 Facture pour *{pending['customer_name']}* — {pending['amount']:,.2f} $\n"
                f"Quel type de service ?",
                parse_mode="Markdown",
                reply_markup=SERVICE_KEYBOARD,
            )
            return

        if result == _QBO_NEEDS_CONFIRMATION:
            await _show_qbo_preview(update, user_id)
            return

        # Si erreur ou client introuvable → message normal
        _add_to_history(user_id, "assistant", result)
        await _send_md(update, result)
        return

    # ── Agents longs → TaskManager (guardrails tokens + timeout) ────────────────
    # Ces agents font des appels Claude lourds → doivent passer par les guardrails
    _TASK_MANAGED_AGENTS = {
        ("framer",    "rédiger"),
        ("blog",      "rédiger"),
        ("analytics", "rapport"),
        ("analytics", "opportunities"),
        ("veille",    "run"),
    }
    if (agent_name, command) in _TASK_MANAGED_AGENTS:
        tm = get_task_manager()
        if tm is not None:
            sujet_label = ctx.get("sujet", f"{agent_name}.{command}")
            tasks_payload = [{
                "agent":   agent_name,
                "command": command,
                "context": ctx,
                "sujet":   sujet_label,
            }]
            confirm = await tm.queue_tasks(tasks_payload, chat_id=user_id)
            _add_to_history(user_id, "assistant", confirm)
            if typing_task:
                typing_task.cancel()
            await _send_md(update, confirm)
            return

    # Tous les autres agents
    import asyncio as _asyncio
    try:
        if agent_name == "chat":
            result = await chat_respond(text, history[:-1])
        else:
            # Timeout global de 3 minutes pour éviter les silences infinis
            result = await _asyncio.wait_for(
                dispatch(agent_name, command, ctx),
                timeout=180,
            )
    except _asyncio.TimeoutError:
        result = (
            f"⏱️ *Timeout — opération trop longue (>3 min)*\n\n"
            f"L'agent `{agent_name}.{command}` n'a pas répondu à temps.\n"
            f"Réessaie dans quelques instants."
        )
    except Exception as e:
        log.error(f"handle_message dispatch error: {e}", exc_info=True)
        result = f"❌ Erreur inattendue dans `{agent_name}.{command}` : {e}"

    if typing_task:
        typing_task.cancel()

    if not result:
        result = "❌ Aucune réponse reçue de l'agent."

    _add_to_history(user_id, "assistant", result)
    await _send_md(update, result)


# ── Callbacks inline keyboards ────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère tous les callbacks des inline keyboards QBO."""
    query = update.callback_query
    await query.answer()  # Toujours répondre en premier pour éviter le spinner
    user_id = update.effective_user.id
    data    = query.data
    log.info(f"callback: user={user_id} data={data}")
    try:
        await _handle_callback_inner(update, context, query, user_id, data)
    except Exception as e:
        log.error(f"handle_callback error: {e}", exc_info=True)
        try:
            await query.message.reply_text(f"❌ Erreur interne : {e}")
        except Exception:
            pass


async def _handle_callback_inner(update, context, query, user_id, data):

    # ── Créer brouillon ───────────────────────────────────────────────────────
    if data.startswith("qbo_draft_"):
        await query.edit_message_text("⏳ Création en cours...")
        result = await execute_create(user_id)
        await query.edit_message_text(result, parse_mode="Markdown")

    # ── Créer + envoyer ───────────────────────────────────────────────────────
    elif data.startswith("qbo_send_"):
        await query.edit_message_text("⏳ Création et envoi en cours...")
        result = await execute_send_direct(user_id)
        await query.edit_message_text(result, parse_mode="Markdown")

    # ── Annuler ───────────────────────────────────────────────────────────────
    elif data.startswith("qbo_cancel_"):
        clear_pending(user_id)
        await query.edit_message_text("❌ Facture annulée.")

    # ── Modifier client ───────────────────────────────────────────────────────
    elif data.startswith("qbo_edit_client_"):
        context.user_data["awaiting_edit"] = "client"
        await query.message.reply_text("✏️ Tape le nouveau nom du client :")

    # ── Modifier montant ──────────────────────────────────────────────────────
    elif data.startswith("qbo_edit_amount_"):
        context.user_data["awaiting_edit"] = "amount"
        await query.message.reply_text("✏️ Tape le nouveau montant (ex: 3500) :")

    # ── Sélection service (pills) ─────────────────────────────────────────────
    elif data.startswith("svc_"):
        service_name = SERVICE_MAP.get(data)
        if not service_name:
            await query.answer("Service inconnu.")
            return

        pending = get_pending(user_id)
        if not pending:
            await query.edit_message_text("❌ Session expirée. Recommence la commande.")
            return

        # Mettre à jour le service et générer le numéro + taxes
        pending["service"] = service_name
        if pending.get("inv_num") is None:
            pending["inv_num"]     = _next_invoice_number()
            pending["tax_code_id"] = _get_qbo_tax_code()
            amount = pending["amount"]
            pending["tps"]   = round(amount * 0.05, 2)
            pending["tvq"]   = round(amount * 0.09975, 2)
            pending["total"] = round(amount + pending["tps"] + pending["tvq"], 2)

        store_pending(user_id, pending)

        # Afficher la preview
        preview_text = _format_preview(pending)
        tax_warning = ""
        if pending.get("_tax_warning") or pending.get("tax_code_id") is None:
            tax_warning = "\n\n⚠️ _Aucun TaxCode TPS/TVQ trouvé — facture sans taxe._"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Créer dans QuickBooks",  callback_data=f"qbo_draft_{user_id}"),
                InlineKeyboardButton("📤 Envoyer directement",   callback_data=f"qbo_send_{user_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Modifier client",  callback_data=f"qbo_edit_client_{user_id}"),
                InlineKeyboardButton("✏️ Modifier montant", callback_data=f"qbo_edit_amount_{user_id}"),
            ],
            [
                InlineKeyboardButton("❌ Annuler", callback_data=f"qbo_cancel_{user_id}"),
            ],
        ])

        await query.edit_message_text(
            preview_text + tax_warning,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


# ── Setup de l'application ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcrit les messages vocaux via OpenAI Whisper et les traite comme du texte."""
    if not _is_allowed(update): return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("❌ Impossible de lire le fichier audio.")
        return

    try:
        # 1. Télécharger le fichier audio depuis Telegram
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()

        # 2. Transcrire via OpenAI Whisper
        from openai import AsyncOpenAI
        from config import OPENAI_API_KEY
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        audio_file = io.BytesIO(bytes(audio_bytes))
        audio_file.name = "voice.ogg"  # Whisper a besoin de l'extension

        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="fr",  # Force le français — retire cette ligne pour auto-detect
        )
        text = transcript.text.strip()

        if not text:
            await update.message.reply_text("❌ Whisper n'a pas pu transcrire l'audio.")
            return

        # 3. Confirmer la transcription à JP
        await update.message.reply_text(f"🎙️ _{text}_", parse_mode="Markdown")

        # 4. Traiter la transcription comme un message texte normal
        # (PTB v20 : Message est immutable, on passe le texte directement)
        user_id = update.effective_user.id
        _add_to_history(user_id, "user", text)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        history = _get_history(user_id)
        agent_name, command, ctx, reply = await parse_intent(text, history[:-1])
        if reply:
            await update.message.reply_text(reply)
        if agent_name and agent_name != "chat":
            import asyncio as _asyncio
            try:
                result = await _asyncio.wait_for(dispatch(agent_name, command, ctx), timeout=180)
            except _asyncio.TimeoutError:
                result = f"⏱️ Timeout — `{agent_name}.{command}` n'a pas répondu à temps."
            except Exception as e:
                result = f"❌ Erreur `{agent_name}.{command}` : {e}"
        else:
            result = await chat_respond(text, history[:-1])
        _add_to_history(user_id, "assistant", result)
        await _send_md(update, result)

    except Exception as e:
        log.error(f"handle_voice error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erreur transcription vocale : {e}")


def build_app() -> Application:
    """Construit et retourne l'application Telegram configurée."""
    global _task_manager

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Enregistrer le bot dans le notifier partagé (pour blog_pipeline et autres)
    from core.telegram_notifier import set_bot, notify
    set_bot(app.bot, TELEGRAM_ALLOWED_USER_ID)

    # Initialiser le TaskManager avec le notifier et l'agent Notion
    from core.task_store import TaskStore
    from core.task_manager import TaskManager
    from agents.notion import agent as notion_agent
    _task_manager = TaskManager(
        store=TaskStore(),
        notifier=notify,
        notion_agent=notion_agent,
    )
    log.info("TaskManager initialisé avec guardrails actifs")

    # Commandes fixes
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("status", cmd_status))

    # Commandes agents (enregistrées dynamiquement après découverte)
    agents = discover_agents()
    for agent_name in agents:
        app.add_handler(CommandHandler(agent_name, cmd_agent))
        log.info(f"Telegram: handler /{agent_name} enregistré")

    # Callbacks inline keyboards
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Messages vocaux → Whisper → handle_message
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

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
        BotCommand("status", "Status des tâches parallèles en cours"),
    ] + [
        BotCommand(name, agent.description[:50])
        for name, agent in agents.items()
    ]
    await app.bot.set_my_commands(commands)
    log.info(f"Telegram: {len(commands)} commandes configurées")
