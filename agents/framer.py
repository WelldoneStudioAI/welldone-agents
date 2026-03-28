"""
agents/framer.py — Agent Framer CMS pour le blog de Welldone Studio.

Architecture:
  Telegram → agents/framer.py (Python WebSocket) → Framer CMS
  Connexion directe via wss://api.framer.com/channel/headless-plugin
  Protocole : devalue flat-array + methodInvocation / methodResponse

Collection cible : ERDJzzQHr (Welldone Studio-Blog)
Field map extrait live depuis l'article Wildman (référence).

Images libres de droits:
  - Unsplash API si UNSPLASH_ACCESS_KEY dispo
  - LoremFlickr sinon (aucune clé, CC)
"""
import asyncio
import json
import logging
import re
import urllib.request
import urllib.error
import urllib.parse

try:
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

from agents._base import BaseAgent
from core.brain import get_client
from config import CLAUDE_MODEL, FRAMER_API_KEY, FRAMER_COLLECTION_ID, UNSPLASH_ACCESS_KEY

log = logging.getLogger(__name__)

# ── Framer WebSocket constants ─────────────────────────────────────────────────
# Project ID : 20 chars après '--' dans l'URL du projet Framer
# https://framer.com/projects/Welldone-Studio--nghGT4Mav9pHCoHxYhyn-cuMch
FRAMER_PROJECT_ID = "nghGT4Mav9pHCoHxYhyn"
FRAMER_WS_URL = (
    f"wss://api.framer.com/channel/headless-plugin"
    f"?projectId={FRAMER_PROJECT_ID}&sdkVersion=0.1.4"
)

# ── Field map confirmé via Wildman Wilderness (référence live) ─────────────────
# IDs confirmés en inspectant les items de ERDJzzQHr via le SDK Node.js
FIELD_MAP: dict[str, dict] = {
    "Title":                {"id": "dAZk2Jaon", "type": "string"},
    "Sous-Titre (gauche)":  {"id": "b3XlDEEmG", "type": "string"},
    "Link":                 {"id": "gR2nhp5qm", "type": "link"},
    "Localisation":         {"id": "y7hP7y7TX", "type": "string"},
    "Secteur d'activité":   {"id": "RrOlspu9Q", "type": "string"},
    "Type de Mandat":       {"id": "XbJge9Fsp", "type": "string"},
    "Objectif Stratégique": {"id": "wQ1Rpjq3x", "type": "string"},
    "Hero-Image":           {"id": "XpFWjsiiE", "type": "image"},
    "Heading1-Titre":       {"id": "dzQTLJWic", "type": "string"},
    "Heading1-Text":        {"id": "Fv1GqGRfr", "type": "string"},
    "Image 2":              {"id": "F1KVBlC4y", "type": "image"},
    "Image 3":              {"id": "zajOvbGoQ", "type": "image"},
    "Image 4":              {"id": "slJKroNUw", "type": "image"},
    "Heading2-Titre":       {"id": "kfYszWeg9", "type": "string"},
    "Heading2-Text":        {"id": "G7VRjLA8G", "type": "string"},
    "Heading3-Titre":       {"id": "YBOUfrYdB", "type": "string"},
    "Heading3-Text":        {"id": "b4tiQybAd", "type": "string"},
    "Heading4-Titre":       {"id": "n9KbxDfwr", "type": "string"},
    "Heading4-Text":        {"id": "lrT2Q_t6E", "type": "string"},
    "Heading5-Titre":       {"id": "b6XLPf15f", "type": "string"},
    "Heading5-Text":        {"id": "vfxlyJSZz", "type": "string"},
    "Image 5":              {"id": "pXmRpf_lU", "type": "image"},
    "Image 6":              {"id": "l2NBo7UWA", "type": "image"},
    "Image 7":              {"id": "SUlGM7z6N", "type": "image"},
    "Heading 3":            {"id": "Na0xhxmje", "type": "string"},
    "Body Text 3":          {"id": "nEx8XU81L", "type": "string"},
    "Body Text 3.2":        {"id": "BRseSZPDu", "type": "string"},
    "Image 8":              {"id": "YpT3cvwNm", "type": "image"},
    "FAQ – Question 1":     {"id": "OucoDUd53", "type": "string"},
    "FAQ – Réponse 1":      {"id": "OQKuM7SLn", "type": "string"},
    "FAQ – Question 2":     {"id": "dA7EIwGaM", "type": "string"},
    "FAQ – Réponse 2":      {"id": "KfaYcUqmL", "type": "string"},
    "FAQ – Question 3":     {"id": "MVXR35XEJ", "type": "string"},
    "FAQ – Réponse 3":      {"id": "eDcRpeq9m", "type": "string"},
    "FAQ – Question 4":     {"id": "awcpjOriL", "type": "string"},
    "FAQ – Réponse 4":      {"id": "y_1oZtBxk", "type": "string"},
    "Content":              {"id": "iSDqww4KB", "type": "formattedText"},
    "CTA 2":                {"id": "mz5FU6wc1", "type": "link"},
}

IMAGE_FIELDS = ["Hero-Image", "Image 2", "Image 3", "Image 4",
                "Image 5", "Image 6", "Image 7", "Image 8"]


# ══════════════════════════════════════════════════════════════════════════════
# devalue protocol (reverse-engineered from framer-api npm package)
# ══════════════════════════════════════════════════════════════════════════════

def devalue_encode(obj) -> str:
    """
    Encode un objet Python en format devalue (flat-array).

    Format : [root_with_indices, val1, val2, ...]
    - Dans les objets : valeurs = indices INTEGER vers flat[]
    - Dans les tableaux : éléments = indices INTEGER vers flat[]
    - Les primitifs au niveau supérieur sont des littéraux

    Exemple :
      {type:"methodInvocation", methodName:"getCollections", id:1, args:[]}
      → [{"type":1,"methodName":2,"id":3,"args":4},
         "methodInvocation","getCollections",1,[]]
    """
    flat: list = []

    def _add(v):
        if isinstance(v, dict):
            idx = len(flat)
            flat.append(None)          # placeholder
            encoded = {k: _add(val) for k, val in v.items()}
            flat[idx] = encoded
            return idx
        elif isinstance(v, list):
            idx = len(flat)
            flat.append(None)          # placeholder
            encoded = [_add(item) for item in v]
            flat[idx] = encoded
            return idx
        else:
            # Primitive : str, int, float, bool, None
            idx = len(flat)
            flat.append(v)
            return idx

    _add(obj)
    return json.dumps(flat, ensure_ascii=False, separators=(",", ":"))


def devalue_decode(s: str):
    """
    Décode une string devalue (flat-array) en objet Python.

    Règle : dans un objet du flat[], les valeurs sont des INDICES ;
            dans un tableau du flat[], les éléments sont des INDICES ;
            les primitifs directs sont des littéraux.
    """
    flat = json.loads(s)
    if not isinstance(flat, list) or not flat:
        return flat

    def _resolve(x):
        if isinstance(x, dict):
            result = {}
            for k, v in x.items():
                if isinstance(v, int) and 0 <= v < len(flat):
                    result[k] = _resolve(flat[v])
                else:
                    result[k] = v   # valeur non-index (fallback)
            return result
        elif isinstance(x, list):
            result = []
            for i in x:
                if isinstance(i, int) and 0 <= i < len(flat):
                    result.append(_resolve(flat[i]))
                else:
                    result.append(i)
            return result
        else:
            return x  # primitif littéral

    return _resolve(flat[0])


# ══════════════════════════════════════════════════════════════════════════════
# Framer WebSocket client
# ══════════════════════════════════════════════════════════════════════════════

class FramerClient:
    """
    Client WebSocket pur-Python pour l'API Framer CMS.

    Utilise le protocole devalue flat-array.
    Context manager async : async with FramerClient(api_key) as c: ...
    """

    def __init__(self, api_key: str):
        self.api_key   = api_key
        self._ws       = None
        self._call_id  = 0

    # ── Context manager ────────────────────────────────────────────────────────
    async def __aenter__(self):
        await self._connect()
        return self

    async def __aexit__(self, *_):
        await self._close()

    # ── Connexion ──────────────────────────────────────────────────────────────
    async def _connect(self):
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Origin":        "https://framer.com",
        }
        self._ws = await websockets.connect(
            FRAMER_WS_URL,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,   # évite les pings inattendus pendant les appels longs
        )
        # Attendre le message "ready" du serveur
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=12)
            decoded = devalue_decode(raw)
            log.debug(f"Framer WS prêt: {decoded}")
        except Exception as e:
            log.warning(f"Framer WS ready-wait: {e}")

    async def _close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Invocation RPC ─────────────────────────────────────────────────────────
    async def invoke(self, method_name: str, *args, timeout: float = 30.0):
        """
        Invoque une méthode Framer CMS via WebSocket et retourne le résultat.
        Lève ValueError en cas d'erreur serveur.
        """
        self._call_id += 1
        cid = self._call_id

        msg     = {"type": "methodInvocation", "methodName": method_name,
                   "id": cid, "args": list(args)}
        encoded = devalue_encode(msg)
        await self._ws.send(encoded)

        # Attendre la réponse correspondante (ignorer les autres messages push)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"{method_name} timeout après {timeout}s")
            try:
                raw     = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                decoded = devalue_decode(raw)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(f"{method_name} timeout après {timeout}s")
            except Exception as e:
                raise ValueError(f"Framer WS recv error: {e}")

            if isinstance(decoded, dict) and decoded.get("id") == cid:
                if "error" in decoded:
                    raise ValueError(f"Framer error [{method_name}]: {decoded['error']}")
                return decoded.get("result")
            # Autre message (push, keepalive) → ignorer et attendre

    # ── Méthodes CMS ──────────────────────────────────────────────────────────
    async def get_collections(self):
        return await self.invoke("getCollections", timeout=20)

    async def get_items(self, collection_id: str):
        return await self.invoke("getCollectionItems2", collection_id, timeout=30)

    async def add_items(self, collection_id: str, items: list):
        return await self.invoke("addCollectionItems2", collection_id, items, timeout=45)

    async def remove_items(self, collection_id: str, item_ids: list):
        """item_ids : liste de dicts {id: str}"""
        return await self.invoke("removeCollectionItems", collection_id, item_ids, timeout=20)


# ══════════════════════════════════════════════════════════════════════════════
# Opérations Framer de haut niveau
# ══════════════════════════════════════════════════════════════════════════════

async def _framer_op(coro) -> dict:
    """Wrapper commun : gestion timeout + exceptions."""
    if not _WS_AVAILABLE:
        return {"ok": False, "error": "Module 'websockets' non installé (pip install websockets)"}
    if not FRAMER_API_KEY:
        return {"ok": False, "error": "FRAMER_API_KEY manquant dans les variables Railway"}
    try:
        result = await asyncio.wait_for(coro, timeout=75)
        return {"ok": True, "data": result}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timeout 75s — Framer CMS inaccessible"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def framer_list_items() -> dict:
    """Retourne {ok, items, count} ou {ok:False, error}."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.get_items(FRAMER_COLLECTION_ID)

    res = await _framer_op(_do())
    if not res["ok"]:
        return res

    raw_items = res["data"] or []
    simplified = []
    for item in (raw_items if isinstance(raw_items, list) else []):
        fd    = item.get("fieldData") or {}
        title = None
        for fid, field in fd.items():
            if isinstance(field, dict) and field.get("type") == "string" and field.get("value"):
                title = field["value"]
                break
        simplified.append({
            "id":        item.get("id"),
            "slug":      item.get("slug"),
            "title":     title or item.get("slug") or "(sans titre)",
            "published": item.get("published", False),
        })
    return {"ok": True, "items": simplified, "count": len(simplified)}


async def framer_add_item(slug: str, field_data: dict) -> dict:
    """Retourne {ok, message} ou {ok:False, error}."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.add_items(FRAMER_COLLECTION_ID, [{"slug": slug, "fieldData": field_data}])

    res = await _framer_op(_do())
    if not res["ok"]:
        return res
    return {"ok": True, "message": "Article créé dans Framer CMS", "result": res["data"]}


async def framer_delete_item(item_id: str) -> dict:
    """Retourne {ok, message} ou {ok:False, error}."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.remove_items(FRAMER_COLLECTION_ID, [{"id": item_id}])

    res = await _framer_op(_do())
    if not res["ok"]:
        return res
    return {"ok": True, "message": f"Article {item_id} supprimé"}


# ══════════════════════════════════════════════════════════════════════════════
# Images libres de droits
# ══════════════════════════════════════════════════════════════════════════════

def _search_unsplash(queries: list[str]) -> list[dict]:
    results = []
    for query in queries[:3]:
        try:
            q   = urllib.parse.quote(query)
            url = f"https://api.unsplash.com/search/photos?query={q}&per_page=3&orientation=landscape"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for photo in data.get("results", []):
                results.append({"src": photo["urls"]["regular"],
                                 "alt": photo.get("alt_description") or query})
                if len(results) >= 8:
                    return results
        except Exception as e:
            log.warning(f"Unsplash '{query}': {e}")
    return results


def _fallback_images(queries: list[str]) -> list[dict]:
    """LoremFlickr — photos CC libres de droits, aucune clé requise."""
    results = []
    for i in range(8):
        q = queries[i % len(queries)].replace(" ", ",") if queries else "business,montreal"
        results.append({"src": f"https://loremflickr.com/1200/800/{q}?lock={i + 1}",
                         "alt": q.replace(",", " ")})
    return results


def _get_images(queries: list[str]) -> list[dict]:
    if UNSPLASH_ACCESS_KEY:
        imgs = _search_unsplash(queries)
        if imgs:
            return imgs
    return _fallback_images(queries)


# ══════════════════════════════════════════════════════════════════════════════
# Slug
# ══════════════════════════════════════════════════════════════════════════════

def _make_slug(text: str) -> str:
    s = text.lower().strip()
    for src, dst in [("é","e"),("è","e"),("ê","e"),("à","a"),("â","a"),("ô","o"),
                     ("î","i"),("û","u"),("ù","u"),("ç","c"),("ë","e"),("ï","i")]:
        s = s.replace(src, dst)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


# ══════════════════════════════════════════════════════════════════════════════
# Prompt de génération Claude
# ══════════════════════════════════════════════════════════════════════════════

_GENERATION_PROMPT = """\
Tu génères un article complet pour le blog de Welldone Studio (awelldone.studio/journal/).

IDENTITÉ DE MARQUE :
- Agence créative montréalaise — photographie commerciale, vidéo, branding, stratégie numérique
- Client cible : entrepreneurs et PME du Québec
- Tagline : "L'image comme actif stratégique"

STYLE : Français québécois professionnel. Direct, concret, paragraphes courts (3-4 lignes max).
Toujours ramener à l'impact business. Exemples concrets. Chiffres si possible.

SUJET : {sujet}

Retourne UNIQUEMENT ce JSON (aucun texte avant/après, aucun markdown) :

{{
  "slug": "slug-url-safe-en-francais-max-80-chars",
  "Title": "Titre H1 accrocheur (10-14 mots, mot-clé principal inclus)",
  "Sous-Titre (gauche)": "Chapeau 2-3 phrases. Accroche forte. Contexte immédiat. Pourquoi lire cet article?",
  "Link": "/services",
  "Localisation": "Montréal, Québec",
  "Secteur d'activité": "Secteur pertinent (ex: Photographie commerciale, Marketing PME, Branding)",
  "Type de Mandat": "Type de contenu (ex: Article SEO, Guide pratique, Étude de cas, Analyse)",
  "Objectif Stratégique": "But marketing (ex: Acquisition PME Montréal, Notoriété SEO, Conversion)",

  "image_queries": ["english keyword 1 for unsplash", "english keyword 2", "english keyword 3"],
  "Hero-Image:alt": "Description de l'image hero idéale pour ce sujet",

  "Heading1-Titre": "Titre section 1 — contexte et problématique concrète",
  "Heading1-Text": "Texte section 1 (380-440 mots). Établit le problème business réel. Exemples. Statistiques si pertinent.",

  "Image 2:alt": "Description image 2 en contexte de l'article",
  "Image 3:alt": "Description image 3",
  "Image 4:alt": "Description image 4",

  "Heading2-Titre": "Titre section 2 — solution ou approche stratégique",
  "Heading2-Text": "Texte section 2 (260-300 mots). La solution Welldone. Pourquoi ça marche.",

  "Heading3-Titre": "Titre section 3 — exemples et résultats concrets",
  "Heading3-Text": "Texte section 3 (260-300 mots). Cas concrets, avant/après, résultats mesurables.",

  "Heading4-Titre": "Titre section 4 — guide pratique ou mise en oeuvre",
  "Heading4-Text": "Texte section 4 (220-260 mots). Étapes concrètes. Ce que JP fait pour ses clients.",

  "Heading5-Titre": "Titre section 5 — impact business et ROI",
  "Heading5-Text": "Texte section 5 (160-200 mots). Chiffres, ROI, valeur à long terme.",

  "Image 5:alt": "Description image 5",
  "Image 6:alt": "Description image 6",
  "Image 7:alt": "Description image 7",

  "Heading 3": "Titre section bonus (question longue traîne SEO, format: Comment... / Pourquoi...)",
  "Body Text 3": "Texte section bonus (200-240 mots). Angle SEO complémentaire.",
  "Body Text 3.2": "Texte complémentaire section bonus (120-160 mots). Conseil pratique additionnel.",

  "Image 8:alt": "Description image 8",

  "FAQ – Question 1": "Question FAQ 1 (avec mot-clé, format interrogatif complet)",
  "FAQ – Réponse 1": "Réponse concise 2-3 phrases. Directe et actionnable.",
  "FAQ – Question 2": "Question FAQ 2",
  "FAQ – Réponse 2": "Réponse concise.",
  "FAQ – Question 3": "Question FAQ 3",
  "FAQ – Réponse 3": "Réponse concise.",
  "FAQ – Question 4": "Question FAQ 4",
  "FAQ – Réponse 4": "Réponse concise.",

  "Content": "",
  "CTA 2": "https://awelldone.studio/contact"
}}"""


# ══════════════════════════════════════════════════════════════════════════════
# Agent Framer
# ══════════════════════════════════════════════════════════════════════════════

class FramerAgent(BaseAgent):
    name        = "framer"
    description = "Rédiger et publier des articles de blog dans Framer CMS"

    @property
    def commands(self):
        return {
            "rédiger":   self.rediger,
            "liste":     self.liste,
            "supprimer": self.supprimer,
        }

    async def rediger(self, context: dict | None = None) -> str:
        """
        Génère un article complet (47 champs) et le publie dans Framer CMS.
        context: { sujet: str }
        """
        ctx   = context or {}
        sujet = ctx.get("sujet", ctx.get("message", "")).strip()

        if not sujet:
            return ("❌ Paramètre `sujet` manquant. "
                    "Ex: « Rédige un article sur comment choisir son photographe corporatif à Montréal »")
        if not FRAMER_API_KEY:
            return "❌ `FRAMER_API_KEY` manquant dans les variables Railway."

        # ── 1. Générer le contenu structuré avec Claude ────────────────────────
        log.info(f"framer.rediger: génération pour « {sujet[:60]} »")
        try:
            resp = get_client().messages.create(
                model=CLAUDE_MODEL,
                max_tokens=6000,
                messages=[{"role": "user", "content": _GENERATION_PROMPT.format(sujet=sujet)}],
            )
            raw = resp.content[0].text.strip()
        except Exception as e:
            return f"❌ Erreur Claude: {e}"

        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())

        try:
            article = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]+\}", raw)
            try:
                article = json.loads(m.group(0)) if m else {}
            except Exception:
                article = {}

        if not article:
            return "❌ Claude n'a pas retourné un JSON valide. Réessaie."

        # ── 2. Images libres de droits ─────────────────────────────────────────
        img_queries = article.get("image_queries",
                                  [sujet, "professional photography Quebec", "business Montreal"])
        images     = _get_images(img_queries)
        img_source = "Unsplash" if UNSPLASH_ACCESS_KEY else "LoremFlickr"

        for i, field in enumerate(IMAGE_FIELDS):
            if i < len(images):
                alt_key     = f"{field}:alt"
                article[field] = {
                    "src": images[i]["src"],
                    "alt": article.get(alt_key) or images[i].get("alt", ""),
                }

        # ── 3. Construire le fieldData Framer (IDs exacts) ─────────────────────
        slug       = _make_slug(article.get("slug") or article.get("Title") or sujet)
        field_data: dict = {}

        for col_name, meta in FIELD_MAP.items():
            fid   = meta["id"]
            ftype = meta["type"]

            if ftype == "image":
                val = article.get(col_name)
                if isinstance(val, dict) and val.get("src"):
                    # Framer attend une string URL (pas un objet {src, alt})
                    field_data[fid] = {"value": val["src"], "type": "image"}
            elif ftype == "formattedText":
                val = article.get(col_name, "")
                if val:
                    field_data[fid] = {"value": f"<p>{val}</p>", "type": "formattedText"}
            elif ftype == "link":
                val = article.get(col_name, "")
                if val:
                    field_data[fid] = {"value": val, "type": "link"}
            else:
                val = article.get(col_name, "")
                if val:
                    field_data[fid] = {"value": str(val), "type": "string"}

        # ── 4. Push vers Framer CMS via WebSocket Python ───────────────────────
        log.info(f"framer.rediger: push slug={slug} fields={len(field_data)}")
        result    = await framer_add_item(slug, field_data)
        titre     = article.get("Title", sujet)
        img_count = len([f for f in IMAGE_FIELDS if article.get(f)])

        if result.get("ok"):
            return (
                f"✅ *Article publié dans Framer CMS*\n\n"
                f"📰 *{titre}*\n"
                f"🔗 Slug: `{slug}`\n"
                f"🖼️ {img_count} images ({img_source})\n"
                f"📋 {len(field_data)} champs remplis sur {len(FIELD_MAP)}\n\n"
                f"👉 awelldone.studio/journal/{slug}"
            )
        else:
            err = result.get("error", "Inconnu")
            log.error(f"framer.rediger push error: {err}")
            return (
                f"⚠️ *Article généré mais NON publié dans Framer*\n\n"
                f"📰 *{titre}*\n"
                f"❌ Erreur: {err[:400]}\n\n"
                f"🔍 Vérifie FRAMER_API_KEY dans Railway."
            )

    async def liste(self, context: dict | None = None) -> str:
        """Liste les articles existants dans Framer CMS."""
        if not FRAMER_API_KEY:
            return "❌ `FRAMER_API_KEY` manquant dans Railway."

        result = await framer_list_items()
        if not result.get("ok"):
            return f"❌ Erreur Framer: {result.get('error', 'Inconnu')}"

        items = result.get("items", [])
        if not items:
            return "📭 Aucun article dans la collection Framer."

        lines = [f"📚 *Articles Framer CMS — {len(items)} au total:*\n"]
        for item in items[:25]:
            status = "🟢 Publié" if item.get("published") else "📝 Brouillon"
            titre  = item.get("title", "Sans titre")
            iid    = item.get("id", "")
            lines.append(f"{status} *{titre}*\n  ID: `{iid}`")

        return "\n".join(lines)

    async def supprimer(self, context: dict | None = None) -> str:
        """Supprime un article par ID. context: {id: str}"""
        item_id = (context or {}).get("id", "").strip()
        if not item_id:
            return "❌ Paramètre `id` manquant. Utilise `framer liste` pour voir les IDs."
        if not FRAMER_API_KEY:
            return "❌ `FRAMER_API_KEY` manquant dans Railway."

        result = await framer_delete_item(item_id)
        if result.get("ok"):
            return f"🗑️ Article `{item_id}` supprimé de Framer CMS."
        return f"❌ Erreur: {result.get('error', 'Inconnu')}"


agent = FramerAgent()
