"""
agents/voyage.py — Agent de voyage créatif.

Architecture en 3 phases codées, pas dépendantes du prompt GPT :
  1. Extraction des paramètres du voyage (GPT-4o → JSON structuré)
  2. Génération du matrix de recherche (Python pur, logique hardcodée)
     - YUL + YQB au départ
     - J-1 / J / J+1 sur les dates
     - Hubs low-cost (billets séparés leg1 + leg2)
  3. Toutes les recherches en parallèle → synthèse GPT-4o → Top 3

Sources de données (ordre de priorité) :
  1. Amadeus Flight Offers API (si AMADEUS_API_KEY + AMADEUS_API_SECRET définis)
     - Inclut : Air Canada, WestJet, Air Transat, American, United, Delta, Sunwing, Porter
     - Signup gratuit → https://developers.amadeus.com/register
  2. SerpAPI Google Flights (fallback si SERPAPI_KEY défini)
     - Utilisé avec deep_search=true pour plus de résultats
     - Manque : Spirit, Flair, Frontier (ne distribuent via aucune API tierce)

Pour Spirit/Flair/Frontier : seul Kiwi.com Tequila les capture (virtual interlining).
Ajouter AMADEUS_API_KEY + AMADEUS_API_SECRET dans Railway quand disponibles.

Requiert : OPENAI_API_KEY + (AMADEUS_API_KEY | SERPAPI_KEY) dans les variables d'env.
"""
import asyncio, json, logging, os, re, time, requests
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
{{
  "origin": "YUL",
  "destination": "SXM",
  "outbound_date": "YYYY-MM-DD",
  "return_date": "YYYY-MM-DD ou null",
  "missing": ""
}}

RÈGLES CRITIQUES :
- Aujourd'hui c'est le {today} (année {year})
- Toutes les dates DOIVENT être dans le futur par rapport à aujourd'hui
- Si l'utilisateur dit "15 mai" sans préciser l'année, utilise {year} si la date est future, sinon {next_year}
- Si les dates sont manquantes, pose UNE question dans "missing"
- origin défaut = YUL (Montréal)"""


async def _extract_params(client, query: str) -> dict:
    """Phase 1 : GPT-4o extrait les paramètres structurés du message naturel."""
    today     = date.today()
    next_year = today.year + 1
    prompt    = EXTRACT_PROMPT.format(
        today=today.isoformat(),
        year=today.year,
        next_year=next_year,
    )
    resp = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=300,
        temperature=0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user",   "content": query},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    params = json.loads(raw)

    # Validation : corriger les dates passées automatiquement
    for field in ("outbound_date", "return_date"):
        val = params.get(field)
        if not val:
            continue
        d = date.fromisoformat(val)
        if d < today:
            params[field] = d.replace(year=d.year + 1).isoformat()
            log.warning(f"voyage: date {val} dans le passé → corrigée à {params[field]}")

    log.info(f"voyage: params extraits = {params}")
    return params


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

    departures = list(dict.fromkeys([origin, "YUL", "YQB"]))[:2]  # max 2 aéroports départ
    offsets    = [-1, 0, 1]   # J-1, J, J+1
    hubs       = _get_hubs(destination)[:2]  # Top 2 hubs low-cost
    searches   = []

    # ── Vols directs/packagés : 2 aéroports × 3 dates ────────────────────────
    for dep in departures:
        for offset in offsets:
            ob  = (outbound + timedelta(days=offset)).isoformat()
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


# ── Phase 3 : Appel Amadeus ───────────────────────────────────────────────────

# Cache du token OAuth Amadeus (valide ~29 min)
_amadeus_token_cache: dict = {"token": "", "expires_at": 0.0}


def _get_amadeus_token() -> str:
    """Récupère le token OAuth Amadeus, mis en cache 29 min."""
    now = time.time()
    if _amadeus_token_cache["token"] and now < _amadeus_token_cache["expires_at"]:
        return _amadeus_token_cache["token"]

    api_key    = os.environ.get("AMADEUS_API_KEY", "")
    api_secret = os.environ.get("AMADEUS_API_SECRET", "")
    base_url   = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

    if not api_key or not api_secret:
        raise ValueError("AMADEUS_API_KEY ou AMADEUS_API_SECRET manquant")

    resp = requests.post(
        f"{base_url}/v1/security/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     api_key,
            "client_secret": api_secret,
        },
        timeout=10,
    )
    data = resp.json()
    if "error" in data:
        raise ValueError(f"Amadeus auth error: {data.get('error_description', data['error'])}")

    token      = data["access_token"]
    expires_in = int(data.get("expires_in", 1799))

    _amadeus_token_cache["token"]      = token
    _amadeus_token_cache["expires_at"] = now + expires_in - 60  # 1 min de marge

    log.info("voyage: token Amadeus renouvelé")
    return token


def _parse_duration(iso_duration: str) -> str:
    """Convertit PT5H30M → 5h30m."""
    d = iso_duration.upper().replace("PT", "")
    h = re.search(r"(\d+)H", d)
    m = re.search(r"(\d+)M", d)
    parts = []
    if h:
        parts.append(f"{h.group(1)}h")
    if m:
        parts.append(f"{m.group(1)}m")
    return "".join(parts) or d


def _search_flights(departure_id: str, arrival_id: str, outbound_date: str,
                    return_date: str | None = None, currency: str = "CAD") -> str:
    """
    Recherche de vols avec fallback automatique :
      1. Amadeus (si clés dispo)  → Air Canada, WestJet, Air Transat, etc.
      2. SerpAPI deep_search=true → Google Flights avec plus de résultats
    """
    if os.environ.get("AMADEUS_API_KEY") and os.environ.get("AMADEUS_API_SECRET"):
        return _search_amadeus(departure_id, arrival_id, outbound_date, return_date, currency)
    elif os.environ.get("SERPAPI_KEY"):
        return _search_serpapi(departure_id, arrival_id, outbound_date, return_date, currency)
    else:
        return "❌ Aucune clé API vol configurée (AMADEUS_API_KEY ou SERPAPI_KEY requis)"


def _search_amadeus(departure_id: str, arrival_id: str, outbound_date: str,
                    return_date: str | None, currency: str) -> str:
    """Amadeus Flight Offers Search API."""
    try:
        token    = _get_amadeus_token()
        base_url = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

        params: dict = {
            "originLocationCode":      departure_id,
            "destinationLocationCode": arrival_id,
            "departureDate":           outbound_date,
            "adults":                  1,
            "currencyCode":            currency,
            "max":                     6,
            "nonStop":                 "false",
        }
        if return_date:
            params["returnDate"] = return_date

        resp = requests.get(
            f"{base_url}/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        data = resp.json()

        if "errors" in data:
            err = data["errors"][0] if data["errors"] else {}
            return f"Erreur Amadeus {departure_id}→{arrival_id}: {err.get('detail', err)}"

        offers = data.get("data", [])
        if not offers:
            return f"Aucun vol {departure_id}→{arrival_id} le {outbound_date}."

        carriers: dict = data.get("dictionaries", {}).get("carriers", {})

        lines = [f"=== {departure_id}→{arrival_id} ({outbound_date}) [Amadeus] ==="]
        for i, offer in enumerate(offers[:5]):
            price    = offer["price"]["grandTotal"]
            cur      = offer["price"]["currency"]
            itin     = offer["itineraries"][0]
            duration = _parse_duration(itin.get("duration", ""))
            segs     = itin["segments"]
            stops    = len(segs) - 1
            airline_codes = list(dict.fromkeys(s["carrierCode"] for s in segs))
            airline_names = [carriers.get(c, c) for c in airline_codes]
            lines.append(
                f"  #{i+1}: {price} {cur} · {duration} · "
                f"{' + '.join(airline_names)} · {stops} escale(s)"
            )

        return "\n".join(lines)

    except ValueError as e:
        return f"❌ Config Amadeus: {e}"
    except Exception as e:
        return f"Erreur Amadeus {departure_id}→{arrival_id}: {e}"


def _search_serpapi(departure_id: str, arrival_id: str, outbound_date: str,
                    return_date: str | None, currency: str) -> str:
    """SerpAPI Google Flights avec deep_search=true pour plus de résultats."""
    try:
        serpapi_key = os.environ.get("SERPAPI_KEY", "")
        flight_type = "1" if return_date else "2"  # 1=aller-retour, 2=aller simple

        params: dict = {
            "engine":        "google_flights",
            "departure_id":  departure_id,
            "arrival_id":    arrival_id,
            "outbound_date": outbound_date,
            "currency":      currency,
            "hl":            "fr",
            "type":          flight_type,
            "deep_search":   "true",   # Plus de combinaisons et résultats
            "api_key":       serpapi_key,
        }
        if return_date:
            params["return_date"] = return_date

        resp = requests.get("https://serpapi.com/search", params=params, timeout=20)
        data = resp.json()

        if "error" in data:
            return f"Erreur SerpAPI: {data['error']}"

        all_flights = data.get("best_flights", []) + data.get("other_flights", [])
        if not all_flights:
            return f"Aucun vol {departure_id}→{arrival_id} le {outbound_date}."

        lines = [f"=== {departure_id}→{arrival_id} ({outbound_date}) [Google Flights] ==="]
        for i, flight in enumerate(all_flights[:5]):
            price    = flight.get("price", "?")
            duration = flight.get("total_duration", 0)
            legs     = flight.get("flights", [])
            airline  = legs[0].get("airline", "?") if legs else "?"
            stops    = len(flight.get("layovers", []))
            lines.append(
                f"  #{i+1}: {price} {currency} · "
                f"{duration // 60}h{duration % 60}m · {airline} · {stops} escale(s)"
            )

        return "\n".join(lines)

    except Exception as e:
        return f"Erreur SerpAPI {departure_id}→{arrival_id}: {e}"


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

        # Vérifier qu'au moins une source de données est configurée
        has_amadeus = bool(os.environ.get("AMADEUS_API_KEY") and os.environ.get("AMADEUS_API_SECRET"))
        has_serpapi = bool(os.environ.get("SERPAPI_KEY"))
        if not has_amadeus and not has_serpapi:
            return (
                "❌ *Aucune source de données vol configurée*\n\n"
                "Ajoute dans Railway au moins une de ces options :\n\n"
                "*Option A — Amadeus (recommandé) :*\n"
                "• `AMADEUS_API_KEY` + `AMADEUS_API_SECRET`\n"
                "Signup → https://developers.amadeus.com/register\n\n"
                "*Option B — SerpAPI (déjà payé) :*\n"
                "• `SERPAPI_KEY`"
            )
        log.info(f"voyage: source={'Amadeus' if has_amadeus else 'SerpAPI deep_search'}")

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
            log.info(f"voyage: phase 3 — {len(searches)} recherches Amadeus en parallèle")
            results = await asyncio.gather(*[
                asyncio.to_thread(_search_flights, **s["api_params"])
                for s in searches
            ])

            for s, r in zip(searches, results):
                log.info(f"voyage: [{s['label']}] → {r[:120]}")

            # ── Phase 4 : Synthèse → Top 3 ───────────────────────────────────
            log.info("voyage: phase 4 — synthèse Top 3")
            return await _synthesize(client, query, searches, results)

        except json.JSONDecodeError:
            return "✈️ Je n'ai pas bien compris les détails du voyage. Réessaie avec : `YUL → SXM 15 mai retour 22 mai`"
        except Exception as e:
            log.error(f"voyage.search error: {e}", exc_info=True)
            return f"⚠️ Erreur agent voyage : {e}"


agent = VoyageAgent()
