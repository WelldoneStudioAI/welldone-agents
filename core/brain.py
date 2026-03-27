"""
core/brain.py — Intent parsing via Claude.

Transforme un message naturel en (agent_name, command, context).
Claude est appelé UNIQUEMENT pour les messages non-structurés.
Les commandes slash (/gmail read) sont parsées directement sans appel API.
"""
import json, logging
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

log = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = """Tu es le cerveau de l'assistant IA de Welldone Studio (Jean-Philippe Roy, Montréal).

Tu dois analyser les messages et retourner UN JSON avec:
{
  "agent": "gmail|calendar|notion|analytics|zoho|veille|chat",
  "command": "sous-commande spécifique",
  "context": {paramètres nécessaires},
  "reply": "message court à afficher à l'utilisateur avant d'exécuter"
}

Agents disponibles:
- gmail: {read, send, search} → emails, contacts
- calendar: {add, list} → événements Google Calendar
- notion: {task, search} → tâches et pages Notion
- analytics: {rapport, sources, keywords, opportunities} → GA4 + Search Console
- zoho: {list, send} → factures Zoho Books
- veille: {run} → veille de contenu hebdomadaire
- voyage: {search} → recherche de vols optimaux (GPT-4o + Google Flights)
- chat: {respond} → conversation générale, rédaction, brainstorm

Pour "send" gmail, context doit avoir: to, subject, body (et optionnellement signature_type)
Pour "add" calendar, context doit avoir: title, date (YYYY-MM-DD), et optionnellement time (HH:MM)
Pour "task" notion, context doit avoir: title, et optionnellement priority, date, notes
Pour "list" zoho, context peut avoir: search (nom client), status
Pour "send" zoho, context doit avoir: search (nom client) ou invoice_id
Pour "search" voyage, context doit avoir: query (description naturelle du voyage, ex: "YUL SXM 15 mai retour 22 mai")
Pour "chat", context doit avoir: message (le texte original)

RÈGLE: Retourne UNIQUEMENT le JSON, sans markdown, sans explication."""


async def parse_intent(
    message: str,
    conversation_history: list[dict],
) -> tuple[str, str, dict, str]:
    """
    Analyse un message naturel et retourne (agent, command, context, reply).

    Args:
        message: Message de l'utilisateur
        conversation_history: Historique des échanges (max 20 msgs)

    Returns:
        (agent_name, command, context_dict, reply_message)
    """
    history = conversation_history[-18:]  # Garder les 18 derniers + le nouveau

    try:
        resp = get_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=history + [{"role": "user", "content": message}],
        )
        raw = resp.content[0].text.strip()

        # Nettoyer si Claude entoure le JSON de markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data    = json.loads(raw)
        agent   = data.get("agent", "chat")
        command = data.get("command", "respond")
        context = data.get("context", {})
        reply   = data.get("reply", "")

        log.info(f"brain: intent={agent}.{command} reply={reply[:50]}")
        return agent, command, context, reply

    except json.JSONDecodeError as e:
        log.error(f"brain: JSON parse error: {e} — raw: {raw[:200]}")
        return "chat", "respond", {"message": message}, ""
    except Exception as e:
        log.error(f"brain: error: {e}")
        return "chat", "respond", {"message": message}, ""


async def chat_respond(message: str, history: list[dict]) -> str:
    """
    Répond à un message de conversation générale.
    Appelé quand agent="chat".
    """
    try:
        sys_prompt = """Tu es l'assistant IA de Jean-Philippe Roy, fondateur de Welldone Studio à Montréal.
Tu l'aides avec la stratégie, la rédaction, les idées, et la gestion quotidienne de son studio.
Ton ton : direct, concis, professionnel. En français québécois naturel."""

        resp = get_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=sys_prompt,
            messages=history[-18:] + [{"role": "user", "content": message}],
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"brain.chat error: {e}")
        return f"❌ Erreur Claude: {e}"
