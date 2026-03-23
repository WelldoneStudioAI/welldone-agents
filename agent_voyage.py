import json
import asyncio
import requests

SERPAPI_KEY = "f68dbc2fbf65738e716bb959db5ae8c4d01a6ca45a1410bda518b4763537b7c9"

VOYAGE_SYSTEM_PROMPT = """
# Agent IA — Optimiseur d’itinéraires ultra-efficients (vols + transport combiné)

## 1.0 Vision générale
Tu es un stratège d’itinéraire expert capaible de trouver, comparer et recommander le trajet global le plus intelligent (prix total réel, durée totale porte à porte, qualité, fatigue, risque opérationnel).

Tu DOIS utiliser l'outil `search_google_flights` pour vérifier les prix réels en direct. 
NE SOUMETS JAMAIS tes 4 recommandations finales sans avoir interrogé les prix réels sur Internet vis cet outil !

## 2.0 Mode Conversationnel
L'utilisateur te parle via Telegram. 
1. Si la demande manque d'infos (point de départ, destination exacte, dates), **NE GÉNÈRE PAS LE RAPPORT ET N'UTILISE PAS L'API DE VOLS**. Pose 1 ou 2 questions de clarification très précises.
2. Mémorisation : Tu as accès à l'historique récent de la conversation. 

## 3.0 Sorties obligatoires (UNIQUEMENT QUAND TU AS CHERCHÉ VIA L'API)
Une fois que tu as suffisamment d'éléments et que tu as OBTENU les données de vols en direct via `search_google_flights`, synthétise ta réponse en 4 scénarios distincts :
1. **Option la moins chère** (selon la vraie donnée)
2. **Option la plus efficace (rapide et fluide)**
3. **Option la plus confortable (moins de fatigue/escales)**
4. **Meilleur compromis intermédiaire**

Pour chaque option : Titre court, Prix total réel, Durée totale courante, Segments clés (et correspondances), Avantages/Inconvénients, Niveau de risque.
Termine par une Recommandation claire et argumentée. N'hésite pas à donner des Deep Links fictifs pour rediriger l'utilisateur vers Expedia ou Google Flights si besoin.

## 4.0 Consignes strictes API
- Fournis les codes IATA corrects (ex: CDG, YUL, SXM, JFK, YQB) à l'outil.
- Tu peux faire **plusieurs appels outils en parallèle** si tu veux vérifier des dates flexibles (ex: le 12, le 13, le 14) ou des aéroports de départ différents. Maximises tes recherches par lots pour un haut niveau d'optimisation !
"""

def _sync_search_google_flights(departure_id, arrival_id, outbound_date, return_date=None, currency="CAD"):
    params = {
        "engine": "google_flights",
        "departure_id": departure_id,
        "arrival_id": arrival_id,
        "outbound_date": outbound_date,
        "currency": currency,
        "hl": "fr",
        "type": "2" if return_date else "1", 
        "api_key": SERPAPI_KEY
    }
    if return_date:
        params["return_date"] = return_date
        
    try:
        response = requests.get("https://serpapi.com/search", params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        best_flights = data.get("best_flights", [])
        other_flights = data.get("other_flights", [])
        all_flights = best_flights + other_flights
        
        if not all_flights:
            return f"Aucun vol trouvé pour {departure_id} -> {arrival_id} le {outbound_date}."
            
        result = f"--- RÉSULTATS {departure_id} -> {arrival_id} (Départ: {outbound_date}) ---\n"
        for i, flight in enumerate(all_flights[:5]): # Garde 5 vols max pour l'IA
            price = flight.get("price", "Inconnu")
            dur = flight.get("total_duration", 0)
            flights_info = flight.get("flights", [])
            airline = flights_info[0].get("airline", "Inconnue") if flights_info else "Inconnue"
            
            result += f"Option {i+1}: Prix={price} {currency} | Durée={dur} min | Div={airline}\n"
            for leg in flights_info:
                dep_code = leg.get("departure_airport", {}).get("id", "?")
                dep_time = leg.get("departure_airport", {}).get("time", "?")
                arr_code = leg.get("arrival_airport", {}).get("id", "?")
                arr_time = leg.get("arrival_airport", {}).get("time", "?")
                num = leg.get("flight_number")
                result += f"  * Vol {num}: {dep_code} ({dep_time}) -> {arr_code} ({arr_time})\n"
            if "layovers" in flight:
                for lay in flight["layovers"]:
                    result += f"  > Escale: {lay.get('duration')} min à {lay.get('name')}\n"
        return result
    except Exception as e:
        return f"Erreur de recherche vols {departure_id}->{arrival_id} : {e}"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_google_flights",
            "description": "Cherche des vols réels sur Google Flights. Retourne les prix en direct et la durée.",
            "parameters": {
                "type": "object",
                "properties": {
                    "departure_id": {
                        "type": "string",
                        "description": "Code IATA départ (ex: YQB, YUL, CDG)"
                    },
                    "arrival_id": {
                        "type": "string",
                        "description": "Code IATA arrivée (ex: SXM, JFK, ORY)"
                    },
                    "outbound_date": {
                        "type": "string",
                        "description": "Date départ requise (format YYYY-MM-DD)"
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Date retour optionnelle (format YYYY-MM-DD)"
                    },
                    "currency": {
                        "type": "string",
                        "description": "Devise (CAD par défaut)"
                    }
                },
                "required": ["departure_id", "arrival_id", "outbound_date"]
            }
        }
    }
]

async def handle_voyage_request(openai_client, conversation_history: list) -> str:
    messages = [{"role": "system", "content": VOYAGE_SYSTEM_PROMPT}]
    for msg in conversation_history:
        messages.append(msg)
    
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.7
        )
        
        msg = response.choices[0].message
        
        if not msg.tool_calls:
            return msg.content
            
        # Reformatage manuel pour compatibilité stricte SDK OpenAI v1
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                } for tc in msg.tool_calls
            ]
        })
        
        for tool_call in msg.tool_calls:
            if tool_call.function.name == "search_google_flights":
                args = json.loads(tool_call.function.arguments)
                tool_result = await asyncio.to_thread(_sync_search_google_flights, **args)
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_result
                })
                
        second_response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )
        
        return second_response.choices[0].message.content
        
    except Exception as e:
        return f"⚠️ Erreur lors de l'analyse de l'itinéraire : {str(e)}"
