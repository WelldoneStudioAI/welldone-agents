"""
agents/voyage.py — Agent de voyage (recherche de vols).

Utilise GPT-4o (OpenAI) + SerpAPI Google Flights.
C'est un exemple concret d'utilisation des licences OpenAI :
→ OpenAI pour le tool calling / function calling (GPT-4o)
→ Claude pour le raisonnement général et la rédaction

Requiert : OPENAI_API_KEY + SERPAPI_KEY dans les variables d'env.
"""
import asyncio, json, logging, os, requests
from agents._base import BaseAgent

log = logging.getLogger(__name__)

VOYAGE_SYSTEM_PROMPT = """
Tu es un chasseur de vols expert pour Jean-Philippe Roy, basé à Montréal (aéroports : YUL, YQB).

RÈGLE ABSOLUE : Tu dois TOUJOURS faire PLUSIEURS recherches en parallèle pour trouver le meilleur prix réel.
Ne te contente JAMAIS d'une seule recherche — l'utilisateur peut faire ça lui-même en 2 minutes.

STRATÉGIE DE RECHERCHE OBLIGATOIRE :
1. Aéroports alternatifs : toujours tester YUL ET YQB au départ (et les aéroports alternatifs à destination si applicable)
2. Flexibilité de dates : tester la date exacte ET J-1/J+1 pour le départ (minimum 3 dates)
3. Résultat minimum : 4 à 6 appels search_google_flights en parallèle avant de répondre

SYNTHÈSE (après avoir toutes les données) :
Présente 4 scénarios numérotés :
1. 💰 Le moins cher (prix absolu, peu importe les contraintes)
2. ⚡ Le plus rapide/efficace (durée porte-à-porte minimale)
3. 😌 Le plus confortable (moins d'escales, horaires civilisés)
4. 🏆 Meilleur compromis (prix/durée/confort)

Pour chaque option : prix réel CAD · durée totale · aéroport départ · escales · compagnie
Termine par : ✅ Ma recommandation : [option X] parce que [raison courte]

RÈGLE : Si la demande manque d'infos cruciales (destination, dates), pose UNE seule question.
RÈGLE : Codes IATA obligatoires (YUL, YQB, SXM, CDG, JFK, ORY, etc.)
"""

FLIGHTS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_google_flights",
        "description": "Cherche des vols réels sur Google Flights via SerpAPI. Retourne les prix et durées en direct.",
        "parameters": {
            "type": "object",
            "properties": {
                "departure_id":  {"type": "string", "description": "Code IATA départ (ex: YQB, YUL, CDG)"},
                "arrival_id":    {"type": "string", "description": "Code IATA arrivée (ex: SXM, JFK, ORY)"},
                "outbound_date": {"type": "string", "description": "Date départ YYYY-MM-DD"},
                "return_date":   {"type": "string", "description": "Date retour YYYY-MM-DD (optionnel)"},
                "currency":      {"type": "string", "description": "Devise (défaut: CAD)"},
            },
            "required": ["departure_id", "arrival_id", "outbound_date"],
        },
    },
}


def _search_flights(departure_id: str, arrival_id: str, outbound_date: str,
                    return_date: str | None = None, currency: str = "CAD") -> str:
    """Appel synchrone SerpAPI Google Flights."""
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    if not serpapi_key:
        return "❌ SERPAPI_KEY non défini"

    params = {
        "engine":         "google_flights",
        "departure_id":   departure_id,
        "arrival_id":     arrival_id,
        "outbound_date":  outbound_date,
        "currency":       currency,
        "hl":             "fr",
        "type":           "1" if return_date else "2",  # 1=aller-retour, 2=aller simple
        "api_key":        serpapi_key,
    }
    if return_date:
        params["return_date"] = return_date

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
        data = resp.json()

        # SerpAPI retourne parfois des erreurs avec status 200
        if "error" in data:
            log.error(f"SerpAPI error: {data['error']}")
            return f"SerpAPI error: {data['error']}"

        resp.raise_for_status()

        all_flights = data.get("best_flights", []) + data.get("other_flights", [])
        if not all_flights:
            return f"Aucun vol trouvé pour {departure_id} → {arrival_id} le {outbound_date}."

        result = f"--- VOLS {departure_id} → {arrival_id} (départ: {outbound_date}) ---\n"
        for i, flight in enumerate(all_flights[:5]):
            price    = flight.get("price", "Inconnu")
            duration = flight.get("total_duration", 0)
            legs     = flight.get("flights", [])
            airline  = legs[0].get("airline", "?") if legs else "?"
            result  += f"\nOption {i+1}: {price} {currency} | {duration} min | {airline}\n"
            for leg in legs:
                dep = leg.get("departure_airport", {})
                arr = leg.get("arrival_airport", {})
                num = leg.get("flight_number", "?")
                result += f"  ✈ Vol {num}: {dep.get('id','?')} ({dep.get('time','?')}) → {arr.get('id','?')} ({arr.get('time','?')})\n"
            for lay in flight.get("layovers", []):
                result += f"  ⏱ Escale: {lay.get('duration')} min à {lay.get('name')}\n"

        return result

    except Exception as e:
        return f"Erreur SerpAPI {departure_id}→{arrival_id}: {e}"


class VoyageAgent(BaseAgent):
    name        = "voyage"
    description = "Recherche de vols optimaux (prix, durée, confort) via Google Flights"

    @property
    def commands(self):
        return {"search": self.search}

    async def search(self, context: dict | None = None) -> str:
        """
        context attendu:
          query (str) — description naturelle du voyage
          history (list) — historique de conversation [optionnel]
        """
        from openai import AsyncOpenAI

        query   = (context or {}).get("query", "")
        history = (context or {}).get("history", [])

        if not query:
            return (
                "✈️ *Agent Voyage* — Dis-moi où tu veux aller !\n\n"
                "Ex: `/voyage YUL → SXM 15 mai, retour 22 mai`\n"
                "ou: `/voyage Montréal Paris 2 juin sans retour`"
            )

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            return "❌ OPENAI_API_KEY non défini — agent voyage indisponible"

        client   = AsyncOpenAI(api_key=openai_key)
        messages = [{"role": "system", "content": VOYAGE_SYSTEM_PROMPT}]
        messages += history[-10:]  # Garder les 10 derniers échanges pour le contexte
        messages.append({"role": "user", "content": query})

        try:
            # Premier appel — GPT-4o décide s'il a besoin de chercher des vols
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=[FLIGHTS_TOOL],
                tool_choice="auto",
                temperature=0.7,
            )
            msg = response.choices[0].message

            # Pas d'appel outil → GPT-4o pose une question de clarification
            if not msg.tool_calls:
                return msg.content

            # Appel(s) outil → chercher les vols en parallèle
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": tc.type,
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })

            # Exécuter tous les appels en parallèle
            tasks = []
            for tc in msg.tool_calls:
                if tc.function.name == "search_google_flights":
                    args = json.loads(tc.function.arguments)
                    tasks.append((tc.id, tc.function.name, args))

            results = await asyncio.gather(*[
                asyncio.to_thread(_search_flights, **args)
                for _, _, args in tasks
            ])

            for (tc_id, fn_name, _), result in zip(tasks, results):
                log.info(f"voyage: tool result [{fn_name}] = {result[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": fn_name,
                    "content": result,
                })

            # Deuxième appel — synthèse avec les vrais prix
            final = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.7,
            )
            log.info(f"voyage.search done query={query[:50]}")
            return final.choices[0].message.content

        except Exception as e:
            log.error(f"voyage.search error: {e}")
            return f"⚠️ Erreur agent voyage: {e}"


agent = VoyageAgent()
