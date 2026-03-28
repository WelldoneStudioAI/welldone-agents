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
  "agent": "gmail|calendar|notion|analytics|qbo|veille|voyage|email|chat",
  "command": "sous-commande spécifique",
  "context": {paramètres nécessaires},
  "reply": "message court à afficher à l'utilisateur avant d'exécuter"
}

Agents disponibles:
- gmail: {read, send, search, scan_invoices} → emails Google / Gmail
- calendar: {add, list} → événements Google Calendar
- notion: {task, search} → tâches et pages Notion
- analytics: {rapport, sources, keywords, opportunities} → GA4 + Search Console
- qbo: {create, create_client, send, list} → facturation QuickBooks Online
- veille: {run} → veille de contenu hebdomadaire
- voyage: {search} → recherche de vols optimaux (GPT-4o + Google Flights)
- framer: {rédiger, liste, supprimer, collections, publier} → articles de blog Framer CMS (awelldone.studio/journal/)
- email: {trier, lire, chercher, résumer, rédiger, envoyer, filtres, créer_filtre, appliquer_filtres, dossiers} → boîte WHC jptanguay@awelldone.com
- chat: {respond} → conversation générale, rédaction, brainstorm

IMPORTANT — agent "email" vs "gmail":
  → "email" = boîte professionnelle WHC (jptanguay@awelldone.com, awelldone.com)
  → "gmail" = boîte Google (awelldonestudio@gmail.com)
  → Par défaut, si JP dit "mes emails" ou "ma boîte", utilise "email" (WHC = boîte principale)

Pour "trier" email (COMMANDE PRINCIPALE — utilise cette commande par défaut pour toute demande sur les emails importants) :
  - context peut avoir: limit (int, défaut 50), mode (str), unseen_only (bool)
  - modes disponibles:
    → "INBOX_IMPORTANTE" (défaut) → P1+P2 seulement
    → "REPONSES_A_FAIRE" → messages demandant une réponse
    → "ARGENT_ADMIN" → factures, contrats, paiements
    → "OPPORTUNITES" → prospects, partenaires, leads
    → "NETTOYAGE" → ce qu'on peut archiver
  - Si JP dit "mes emails importants", "trie ma boîte", "quoi traiter", "opportunités" → utilise "trier"
  - Si JP dit "emails non lus seulement" → unseen_only: true

Pour "lire" email → liste brute, context peut avoir: limit (int, défaut 15)
Pour "chercher" email → context doit avoir: query (str — expéditeur, sujet ou mot-clé)
Pour "résumer" email → context doit avoir: uid (str — numéro entre crochets, ex: "12345")
Pour "rédiger" email → context doit avoir: to (str), contexte (str — instructions de rédaction)
Pour "envoyer" email → context doit avoir: to (str), subject (str), body (str)
Pour "créer_filtre" email → context doit avoir: description (str — description en langage naturel de la règle)
  - Ex: "si expéditeur contient newsletter → marquer lu et archiver"
  - Ex: "si sujet contient Facture → déplacer vers Comptabilité"
  - Ex: "si expéditeur est tesla.com → priorité haute"
Pour "appliquer_filtres" email → context peut avoir: limit (int, défaut 200)
Pour "filtres" email → liste les filtres actifs (pas de context requis)
Pour "dossiers" email → liste les dossiers IMAP (pas de context requis)

Pour "send" gmail, context doit avoir: to, subject, body (et optionnellement signature_type)
Pour "add" calendar, context doit avoir: title, date (YYYY-MM-DD), et optionnellement time (HH:MM)
Pour "task" notion, context doit avoir: title, et optionnellement priority, date, notes
Pour "create" qbo, context doit avoir: client (nom), amount (float), description (str), et optionnellement client_email si le client est nouveau
  → Si le type de service n'est pas mentionné dans le message, mets description="?" pour que le bot affiche les pills de sélection
  → Services disponibles: "Photographie corporative", "Photographie commerciale", "Service digital", "Consultation stratégique"
  → Si le service est clairement mentionné (ex: "photo", "digital", "consultation"), utilise directement la valeur correspondante
Pour "create_client" qbo, context doit avoir: display_name (str), email (str), et optionnellement phone, address
Pour "send" qbo, context doit avoir: invoice_id OU invoice_num (numéro de facture)
Pour "list" qbo, context peut avoir: status ("unpaid"|"overdue"|"all"), limit (int)
Pour "scan_invoices" gmail, context peut avoir: days (int, défaut 7) pour la période de recherche
Pour "search" voyage, context doit avoir: query (description naturelle du voyage, ex: "YUL SXM 15 mai retour 22 mai")
Pour "rédiger" framer, context doit avoir: sujet (str — idée ou sujet complet de l'article)
  - Ex: "rédige un article sur la valeur de la photo pro pour une PME" → framer.rédiger {sujet: "..."}
  - Si JP mentionne "article de blog", "rédige pour le site", "contenu Framer", "journal" → utilise framer
  - Le sujet doit être le plus complet possible (reprendre la demande entière de JP)
Pour "liste" framer → liste les articles existants (pas de context requis)
Pour "supprimer" framer → context doit avoir: id (str — l'ID de l'article affiché par /framer liste)
Pour "collections" framer → liste toutes les collections Framer du projet (IDs + noms) — utile pour configurer le portfolio
Pour "chat", context doit avoir: message (le texte original)

RÈGLE IMPORTANTE: Si l'utilisateur répond à une question précédente (ex: donne un email après qu'on lui a demandé pour créer un client), utilise l'historique de conversation pour reconstruire le context complet.
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
