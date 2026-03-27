"""
agents/voyage.py — Agent de voyage créatif.

Architecture en 3 phases codées, pas dépendantes du prompt GPT :
  1. Extraction des paramètres du voyage (GPT-4o → JSON structuré)
  2. Génération du matrix de recherche (Python pur, logique hardcodée)
     - YUL + YQB au départ
     - J-1 / J / J+1 sur les dates
     - Hubs low-cost (billets séparés leg1 + leg2)
  3. Toutes les recherches en parallèle → synthèse GPT-4o → Top 3

Requiert : OPENAI_API_KEY + SERPAPI_KEY dans les variables d'env.
"""
import asyncio, json, logging, os, requests
from datetime import date, timedelta
from agents._base import BaseAgent

log = logging.getLogger(__name__)

# ── Hubs low-cost par région de destination ───────────────────────────────────
# Permet de trouver "YUL→MIA (Spirit 80$) + MIA→SXM (AA 95$) = 175$"
# au lieu du vol packagé à 600$

CARIBBEAN_HUBS  = ["MIA", "JFK", "EWR", "ATL", "BOS"]
EUROPE_HUBS     = ["JFK", "YYZ", "ORD"]
MEXICO_HUBS     = ["MIA", "JFK", "ORD", "ATL"]
DEFAULT_HUBS    = ["MIA", "JFK", "ATL"]

DESTINATION_HUBS: dict[str, list[str]] = {
    # Caraïbes
    "SXM": CARIBBEAN_HUBS, "EUM": CARIBBEAN_HUBS,
    "MBJ": CARIBBEAN_HUBS, "KIN": CARIBBEAN_HUBS,
    "PUJ": CARIBBEAN_HUBS, "SDQ": CARIBBEAN_HUBS,
    "ANU": ["MIA", "JFK"],  "BGI": ["MIA", "JFK"],
    "NAS": ["MIA"],          "GCM": ["MIA"],
    "UVF": ["MIA", "JFK"],  "TAB": ["MIA"],
    "CUR": ["MIA"],          "AUA": ["MIA"],
    # Europe
    "CDG": EUROPE_HUBS, "ORY": EUROPE_HUBS,
    "LHR": EUROPE_HUBS, "LGW": EUROPE_HUBS,
    "AMS": EUROPE_HUBS, "FRA": EUROPE_HUBS,
    "MAD": EUROPE_HUBS, "LIS": EUROPE_HUBS,
    "FCO": EUROPE_HUBS, "BCN": EUROPE_HUBS,
    # Mexique
    "CUN": MEXICO_HUBS, "MEX": MEXICO_HUBS,
    "PVR": MEXICO_HUBS, "SJD": MEXICO_HUBS,
}


def _get_hubs(destination: str) -> list[str]:
    return DESTINATION_HUBS.get(destination.upper(), DEFAULT_HUBS)


# ── Phase 1 : Extraction des paramètres ──────────────────────────────────────

EXTRACT_PROMPT = """Extrais les paramètres de voyage de ce message et retourne UNIQUEMENT un JSON valide.

JSON attendu :
{
  "origin": "YUL",           // code IATA départ (défaut YUL si Montréal)
  "destination": "SXM",      // code IATA destination
  "outbound_date": "2026-05-15",  // YYYY-MM-DD
  "return_date": "2026-05-22",    // YYYY-MM-DD ou null si aller simple
  "missing": ""              // si une info cruciale manque, pose UNE question ici
}

Aujourd'hui : {today}
Si l'info est manquante (destination ou dates), mets la question dans "missing" et null ailleurs."""


async def _extract_params(client, query: str) -> dict:
    """Phase 1 : GPT-4o extrait les paramètres structurés du message naturel."""
    today = date.today().isoformat()
    resp = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=300,
        temperature=0,
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT.replace("{today}", today)},
            {"role": "user",   "content": query},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)


# ── Phase 2 : Génération du matrix de recherche ───────────────────────────────

def _generate_searches(params: dict) -> list[dict]:
    """
    Génère toutes les recherches à exécuter en parallèle.
    Returns: liste de dicts avec {label, api_params}
    """
    origin      = params.get("origin", "YUL").upper()
    destination = params.get("destination", "").upper()
    outbound    = date.fromisoformat(params["outbound_date"])
    return_d    = date.fromisoformat(params["return_date"]) if params.get("return_date") else None
    is_roundtrip = return_d is not None

    departures = list(dict.fromkeys([origin, "YUL", "YQB"]))[:2]  # max 2 aéroports départ
    offsets    = [-1, 0, 1]   # J-1, J, J+1
    hubs       = _get_hubs(destination)[:2]  # Top 2 hubs low-cost
    searches   = []

    # ── Vols directs/packagés : 2 aéroports × 3 dates ────────────────────────
    for dep in departures:
        for offset in offsets:
            ob = (outbound + timedelta(days=offset)).isoformat()
            ret = (return_d + timedelta(days=offset)).isoformat() if return_d else None
            label = f"{dep}→{destination} {ob}" + (f" retour {ret}" if ret else " (A/S)")
            searches.append({
                "label": label,
                "type":  "direct",
                "api_params": {
                    "departure_id":  dep,
                    "arrival_id":    destination,
                    "outbound_date": ob,
                    "return_date":   ret,
                },
            })

    # ── Billets séparés via hubs low-cost ─────────────────────────────────────
    # Leg 1 : YUL → Hub (aller simple)
    # Leg 2 : Hub → Destination (aller simple)
    # Le retour serait Hub → YUL (aller simple séparé)
    for hub in hubs:
        ob = outbound.isoformat()
        searches.append({
            "label": f"YUL→{hub} {ob} (leg1)",
            "type":  "hub_leg1",
            "hub":   hub,
            "api_params": {
                "departure_id":  "YUL",
                "arrival_id":    hub,
                "outbound_date": ob,
                "return_date":   None,
            },
        })
        searches.append({
            "label": f"{hub}→{destination} {ob} (leg2)",
            "type":  "hub_leg2",
            "hub":   hub,
            "api_params": {
                "departure_id":  hub,
                "arrival_id":    destination,
                "outbound_date": ob,
                "return_date":   None,
            },
        })
        # Retour si aller-retour
        if return_d:
            ret_str = return_d.isoformat()
            searches.append({
                "label": f"{destination}→{hub} {ret_str} (retour leg1)",
                "type":  "hub_ret1",
                "hub":   hub,
                "api_params": {
                    "departure_id":  destination,
                    "arrival_id":    hub,
                    "outbound_date": ret_str,
                    "return_date":   None,
                },
            })
            searches.append({
                "label": f"{hub}→YUL {ret_str} (retour leg2)",
                "type":  "hub_ret2",
                "hub":   hub,
                "api_params": {
                    "departure_id":  hub,
                    "arrival_id":    "YUL",
                    "outbound_date": ret_str,
                    "return_date":   None,
                },
            })

    log.info(f"voyage: {len(searches)} recherches générées pour {origin}→{destination}")
    return searches


# ── Phase 3 : Appel SerpAPI ───────────────────────────────────────────────────

def _search_flights(departure_id: str, arrival_id: str, outbound_date: str,
                    return_date: str | None = None, currency: str = "CAD") -> str:
    """Appel synchrone SerpAPI Google Flights. Retourne un résumé texte."""
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    if not serpapi_key:
        return "❌ SERPAPI_KEY manquant"

    flight_type = "1" if return_date else "2"  # 1=aller-retour, 2=aller simple
    params = {
        "engine":        "google_flights",
        "departure_id":  departure_id,
        "arrival_id":    arrival_id,
        "outbound_date": outbound_date,
        "currency":      currency,
        "hl":            "fr",
        "type":          flight_type,
        "api_key":       serpapi_key,
    }
    if return_date:
        params["return_date"] = return_date

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
        data = resp.json()

        if "error" in data:
            return f"Erreur: {data['error']}"

        all_flights = data.get("best_flights", []) + data.get("other_flights", [])
        if not all_flights:
            return f"Aucun vol {departure_id}→{arrival_id} le {outbound_date}."

        lines = [f"=== {departure_id}→{arrival_id} ({outbound_date}) ==="]
        for i, flight in enumerate(all_flights[:4]):
            price    = flight.get("price", "?")
            duration = flight.get("total_duration", 0)
            legs     = flight.get("flights", [])
            airline  = legs[0].get("airline", "?") if legs else "?"
            stops    = len(flight.get("layovers", []))
            lines.append(f"  #{i+1}: {price} {currency} · {duration//60}h{duration%60}m · {airline} · {stops} escale(s)")

        return "\n".join(lines)

    except Exception as e:
        return f"Erreur {departure_id}→{arrival_id}: {e}"


# ── Phase 4 : Synthèse ────────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """Tu reçois des résultats de recherche de vols bruts pour un voyage.
Ton rôle : analyser intelligemment et proposer les TOP 3 scénarios les plus intéressants.

IMPORTANT — Pense aux billets séparés :
- Les résultats "leg1" et "leg2" via un hub peuvent être combinés en un trajet total moins cher
- Ex: YUL→MIA 89$ + MIA→SXM 95$ = 184$ aller (à comparer aux vols packagés)
- Additionne les prix des legs pour calculer le coût total du trajet via hub

FORMAT DE RÉPONSE OBLIGATOIRE :

✈️ *Top 3 options — [Origine] → [Destination]*

*1. [Emoji] [Titre court]*
💰 [Prix total] CAD | ⏱ [Durée] | 🛫 [Trajet complet avec hubs si applicable]
🏢 [Compagnie(s)] | ✅ [1 avantage clé] | ⚠️ [1 risque ou inconvénient]

*2. [Emoji] [Titre court]*
[même format]

*3. [Emoji] [Titre court]*
[même format]

🏆 *Ma recommandation : Option [X]*
[1-2 phrases pourquoi c'est le meilleur choix pour ce voyageur]

Note: si des dates alternatives ou YQB donnent de meilleures options, mets-les en avant."""


async def _synthesize(client, query: str, searches: list[dict], results: list[str]) -> str:
    """Phase 4 : GPT-4o analyse tous les résultats et produit le Top 3."""
    # Assembler les résultats en texte structuré
    data_block = "\n\n".join(
        f"[{s['label']}]\n{r}"
        for s, r in zip(searches, results)
    )

    resp = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1200,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {"role": "user",   "content": f"Demande originale : {query}\n\nDonnées de recherche :\n{data_block}"},
        ],
    )
    return resp.choices[0].message.content


# ── Agent ─────────────────────────────────────────────────────────────────────

class VoyageAgent(BaseAgent):
    name        = "voyage"
    description = "Recherche créative de vols — hubs low-cost, billets séparés, Top 3 scénarios"

    @property
    def commands(self):
        return {"search": self.search}

    async def search(self, context: dict | None = None) -> str:
        from openai import AsyncOpenAI

        query = (context or {}).get("query", "")
        if not query:
            return (
                "✈️ *Agent Voyage* — Dis-moi où tu veux aller !\n\n"
                "Ex: `YUL → SXM 15 mai, retour 22 mai`\n"
                "ou: `Montréal → Paris 2 juin sans retour`"
            )

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if not openai_key:
            return "❌ OPENAI_API_KEY non défini"

        client = AsyncOpenAI(api_key=openai_key)

        try:
            # ── Phase 1 : Extraire les paramètres ────────────────────────────
            log.info(f"voyage: phase 1 — extraction paramètres pour: {query[:80]}")
            params = await _extract_params(client, query)

            if params.get("missing"):
                return f"✈️ {params['missing']}"

            if not params.get("destination") or not params.get("outbound_date"):
                return "✈️ Dis-moi la destination et les dates pour que je cherche !"

            log.info(f"voyage: params={params}")

            # ── Phase 2 : Générer le matrix de recherche ──────────────────────
            searches = _generate_searches(params)

            # ── Phase 3 : Exécuter toutes les recherches en parallèle ─────────
            log.info(f"voyage: phase 3 — {len(searches)} recherches en parallèle")
            results = await asyncio.gather(*[
                asyncio.to_thread(_search_flights, **s["api_params"])
                for s in searches
            ])

            for s, r in zip(searches, results):
                log.info(f"voyage: [{s['label']}] → {r[:100]}")

            # ── Phase 4 : Synthèse → Top 3 ───────────────────────────────────
            log.info("voyage: phase 4 — synthèse Top 3")
            return await _synthesize(client, query, searches, results)

        except json.JSONDecodeError:
            return "✈️ Je n'ai pas bien compris les détails du voyage. Réessaie avec : `YUL → SXM 15 mai retour 22 mai`"
        except Exception as e:
            log.error(f"voyage.search error: {e}", exc_info=True)
            return f"⚠️ Erreur agent voyage : {e}"


agent = VoyageAgent()
