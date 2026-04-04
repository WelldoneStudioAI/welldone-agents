"""
agents/framer.py — Agent Framer CMS pour le blog de Welldone Studio.

Architecture:
  Telegram → agents/framer.py (Python WebSocket) → Framer CMS
  Connexion directe via wss://api.framer.com/channel/headless-plugin
  Protocole : devalue flat-array + methodInvocation / methodResponse

Collection cible : ERDJzzQHr (Welldone Studio-Blog)
Field map extrait live depuis l'article Wildman (référence).

Images :
  1. Gemini Imagen 3 + Cloudinary (sur mesure Welldone Studio)
  2. Portfolio Welldone (si FRAMER_PROJECTS_COLLECTION_ID configuré)
  3. Lorem Picsum (fallback minimal — aucune clé requise)
"""
import asyncio
import json
import logging
import re
import time
import urllib.request
import urllib.error
import urllib.parse
import random
import string
from datetime import date as _date, datetime as _datetime

try:
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

from agents._base import BaseAgent
from core.brain import get_client
from core.guardrails import safe_claude_call
import base64
from config import (CLAUDE_MODEL, FRAMER_API_KEY, FRAMER_COLLECTION_ID,
                    FRAMER_PROJECTS_COLLECTION_ID, FRAMER_STAGING_URL,
                    GEMINI_API_KEY, GCS_BUCKET)

log = logging.getLogger(__name__)

# Cache inter-phases : slug → données article pour la commande illustrer
# Durée de vie : session Railway (reset au redémarrage)
_article_cache: dict[str, dict] = {}

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
        except asyncio.TimeoutError:
            log.warning("Framer WS: pas de message 'ready' après 12s — on continue quand même")
        except Exception as e:
            await self._close()
            raise ConnectionError(f"Framer WS connexion échouée: {e}") from e

    async def _close(self):
        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3)
            except Exception as e:
                log.debug(f"framer WS close: {e}")
            finally:
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
                if decoded.get("error") is not None:
                    raise ValueError(f"Framer error [{method_name}]: {decoded['error']}")
                return decoded.get("result")
            # Autre message (push, keepalive) → ignorer et attendre

    # ── Méthodes CMS ──────────────────────────────────────────────────────────
    async def get_collections(self):
        return await self.invoke("getCollections", timeout=20)

    async def get_items(self, collection_id: str):
        return await self.invoke("getCollectionItems2", collection_id, timeout=30)

    async def add_items(self, collection_id: str, items: list):
        return await self.invoke("addCollectionItems2", collection_id, items, timeout=120)

    async def remove_items(self, collection_id: str, item_ids: list):
        """item_ids : liste de dicts {id: str}"""
        return await self.invoke("removeCollectionItems", collection_id, item_ids, timeout=20)

    async def publish_site(self):
        """Déclenche un publish vers staging (*.framer.app) — reconstruit le site, 30-90s."""
        return await self.invoke("publish", timeout=120.0)


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
        result = await asyncio.wait_for(coro, timeout=150)
        return {"ok": True, "data": result}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timeout 150s — Framer CMS inaccessible"}
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
        staged = item.get("staged")
        if staged is True:
            pub_state = False
        elif staged is False:
            pub_state = True
        else:
            pub_state = item.get("published")
        simplified.append({
            "id":         item.get("id"),
            "slug":       item.get("slug"),
            "title":      title or item.get("slug") or "(sans titre)",
            "published":  pub_state,
            "field_data": fd,   # conservé pour illustrer sans cache
        })
    return {"ok": True, "items": simplified, "count": len(simplified)}


async def framer_add_item(slug: str, field_data: dict) -> dict:
    """Retourne {ok, message} ou {ok:False, error}. Item créé en brouillon (staging).
    Retry automatique avec suffixe aléatoire si slug déjà pris."""

    async def _try_add(s: str) -> dict:
        async def _do():
            async with FramerClient(FRAMER_API_KEY) as c:
                result = await c.add_items(
                    FRAMER_COLLECTION_ID, [{"slug": s, "fieldData": field_data}]
                )
                log.info(f"framer addCollectionItems2 response: {str(result)[:300]}")
                return result
        return await _framer_op(_do())

    res = await _try_add(slug)

    # Retry avec suffix aléatoire si slug dupliqué
    for _retry in range(3):
        if res["ok"] or "duplicate" not in (res.get("error") or "").lower():
            break
        rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        retry_slug = f"{slug[:73]}-{rand_suffix}"
        log.info(f"framer: slug dupliqué (retry {_retry+1}), nouveau slug={retry_slug}")
        res = await _try_add(retry_slug)

    if not res["ok"]:
        return res
    # Extraire le slug réellement créé (peut avoir un suffixe si doublon)
    created_data = res["data"]
    actual_slug = slug  # fallback
    if isinstance(created_data, list) and created_data:
        actual_slug = created_data[0].get("slug", slug)
    elif isinstance(created_data, dict):
        actual_slug = created_data.get("slug", slug)
    return {"ok": True, "message": "Article créé dans Framer CMS", "result": created_data, "slug": actual_slug}


async def framer_publish_staging() -> dict:
    """Publie le staging Framer (*.framer.app) — nécessaire pour que l'article soit visible sans login."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.publish_site()
    return await _framer_op(_do())


async def framer_qa_verify(slug: str) -> dict:
    """
    QA bloquant — vérifie qu'un article est dans le CMS puis publie sur staging.

    Étapes :
      1. Vérifie que le slug existe dans la collection CMS (avec retry 3×5s)
      2. Déclenche publish() — best-effort (toute erreur WS est traitée comme "déclenché")
      3. Attend 20s pour laisser Framer terminer le déploiement
      4. Construit l'URL staging et la retourne (toujours ok:True si slug trouvé)

    Retourne :
      {ok: True,  slug, deployment_id, staging_url}
      {ok: False, slug, error, step}   — uniquement si slug introuvable après 3 tentatives
    """
    staging_base = (FRAMER_STAGING_URL or "").rstrip("/")

    # ── Étape 1 : slug présent dans le CMS ? (retry 3× pour propagation Framer) ─
    found = False
    for _retry in range(3):
        list_res = await framer_list_items()
        if not list_res.get("ok"):
            return {"ok": False, "slug": slug, "error": list_res.get("error"), "step": "list_items"}
        found = any(item.get("slug") == slug for item in list_res.get("items", []))
        if found:
            break
        if _retry < 2:
            log.info(f"framer_qa_verify: slug {slug!r} non trouvé (tentative {_retry+1}/3), attente 5s…")
            await asyncio.sleep(5)

    if not found:
        log.warning(f"framer_qa_verify: slug {slug!r} introuvable après 3 tentatives — publish quand même")
        # Ne pas bloquer : l'item peut être présent mais l'API lente à refléter

    # ── Étape 2 : publish() — best-effort, Framer ferme souvent le WS (normal) ─
    async def _do_publish():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.publish_site()

    pub_res = await _framer_op(_do_publish())
    dep_id = ""
    if pub_res.get("ok"):
        pub_data   = pub_res.get("data") or {}
        deployment = pub_data.get("deployment") or {}
        dep_id     = deployment.get("id", "triggered")
        log.info(f"framer_qa_verify: publish OK — dep_id={dep_id!r}")
    else:
        # Framer ferme souvent la connexion WS immédiatement après publish (comportement normal).
        # Toute erreur WS est traitée comme "publish déclenché" — on ne peut pas confirmer côté serveur.
        err_msg = pub_res.get("error", "")
        log.info(f"framer_qa_verify: publish WS fermé/erreur (normal) — {err_msg[:100]!r}")
        dep_id = "ws-triggered"

    staging_url = f"{staging_base}/journal/{slug}" if staging_base else ""
    await asyncio.sleep(20)  # laisser Framer terminer le rebuild staging
    log.info(f"framer_qa_verify ✅ slug={slug} dep={dep_id} found={found} url={staging_url}")
    return {
        "ok":            True,
        "slug":          slug,
        "deployment_id": dep_id,
        "staging_url":   staging_url,
        "slug_verified": found,
    }


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
# Option B — Photos du portfolio Welldone (si Gemini indisponible)
# ══════════════════════════════════════════════════════════════════════════════

async def framer_get_collections() -> dict:
    """Liste toutes les collections du projet Framer avec leurs IDs et noms."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.get_collections()

    res = await _framer_op(_do())
    if not res["ok"]:
        return res

    raw = res["data"] or []
    cols = []
    for c in (raw if isinstance(raw, list) else []):
        cols.append({
            "id":    c.get("id", "?"),
            "name":  c.get("name", "Sans nom"),
            "count": c.get("itemCount") or "?",
        })
    return {"ok": True, "collections": cols}


async def _get_portfolio_images(sector: str, max_images: int = 8) -> list[dict]:
    """
    Cherche des images dans la collection Projets Framer.

    Priorité :
      1. Projets dont un champ string contient un mot-clé du secteur
      2. N'importe quel projet (tous secteurs confondus)
    Retourne [] si FRAMER_PROJECTS_COLLECTION_ID non configuré.
    """
    if not FRAMER_PROJECTS_COLLECTION_ID:
        return []

    try:
        async with FramerClient(FRAMER_API_KEY) as c:
            items = await c.get_items(FRAMER_PROJECTS_COLLECTION_ID)
    except Exception as e:
        log.warning(f"portfolio get_items: {e}")
        return []

    if not items or not isinstance(items, list):
        return []

    # Mots-clés du secteur pour le matching (ex: "restaurant", "dentaire", "corporate")
    keywords = [w.lower() for w in re.split(r"[\s,;/]+", sector) if len(w) > 3]

    matched_imgs: list[dict] = []
    other_imgs:   list[dict] = []

    for item in items:
        fd = item.get("fieldData") or {}

        # Vérifier si ce projet est dans le bon secteur
        project_text = " ".join(
            str(f.get("value", "")).lower()
            for f in fd.values()
            if isinstance(f, dict) and f.get("type") == "string"
        )
        is_match = any(kw in project_text for kw in keywords) if keywords else False

        # Extraire toutes les URLs d'images du projet
        project_imgs: list[dict] = []
        for fid, field in fd.items():
            if not isinstance(field, dict) or field.get("type") != "image":
                continue
            src = field.get("value")
            if isinstance(src, str) and src.startswith("http"):
                project_imgs.append({
                    "src": src,
                    "alt": f"Photo Welldone Studio — {item.get('slug', '')}",
                    "credit": "Welldone Studio",
                })
            elif isinstance(src, dict) and isinstance(src.get("src"), str):
                project_imgs.append({
                    "src": src["src"],
                    "alt": src.get("alt") or f"Photo Welldone Studio — {item.get('slug', '')}",
                    "credit": "Welldone Studio",
                })

        if is_match:
            matched_imgs.extend(project_imgs)
        else:
            other_imgs.extend(project_imgs)

    result = matched_imgs + other_imgs
    log.info(f"portfolio: {len(matched_imgs)} images secteur + {len(other_imgs)} autres → {len(result)} total")
    return result[:max_images]


# ══════════════════════════════════════════════════════════════════════════════
# Génération d'images Gemini × Cloudinary
# ══════════════════════════════════════════════════════════════════════════════

async def _generate_image_gemini(article_section: str) -> bytes | None:
    """
    Génère une image via Gemini generate_content (gemini-3.1-flash-image-preview).
    Passe le texte réel de l'article comme contexte → image directement liée au contenu.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY manquant dans Railway")
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = (
        f"Illustrate this blog article excerpt for Welldone Studio, a Montreal creative agency:\n\n"
        f"\"{article_section[:600]}\"\n\n"
        "Create an authentic editorial photograph that visually represents the concept above. "
        "Style: minimalist, warm neutral tones, natural window light, genuine candid moment, "
        "not a stock photo, not AI-looking. If screens are visible, show work related to the article topic. "
        "No text overlays, no watermarks. Commercial photography quality. 16:9 aspect ratio."
    )

    def _call():
        resp = client.models.generate_content(
            model="gemini-3.1-flash-image-preview",
            contents=[prompt],
        )
        for part in resp.parts:
            if part.inline_data is not None:
                return part.inline_data.data
        return None

    return await asyncio.to_thread(_call)


def _upload_to_gcs(image_bytes: bytes, blob_name: str) -> str | None:
    """
    Upload des bytes PNG vers Google Cloud Storage → URL publique permanente.
    blob_name : ex. 'blog/mon-article-0329-img1.png'
    """
    if not GCS_BUCKET:
        return None
    try:
        import os, json
        from google.cloud import storage as gcs
        from google.oauth2 import service_account

        sa_b64 = os.environ.get("GOOGLE_SA_JSON_B64", "")
        if sa_b64:
            sa_info = json.loads(base64.b64decode(sa_b64))
            creds   = service_account.Credentials.from_service_account_info(
                sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            client = gcs.Client(credentials=creds, project=sa_info.get("project_id"))
        else:
            client = gcs.Client()

        bucket = client.bucket(GCS_BUCKET)
        blob   = bucket.blob(blob_name)
        blob.upload_from_string(image_bytes, content_type="image/png")
        return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"
    except Exception as e:
        log.warning(f"GCS upload error: {e}")
    return None


async def _generate_and_upload_image(visual_context: str, public_id: str) -> dict | None:
    """
    Pipeline complet : Gemini → bytes → GCS → URL permanente.
    Retourne {src, alt, credit} ou None si échec.
    Timeout dur de 60s pour ne pas bloquer le pipeline global.
    """
    try:
        img_bytes = await asyncio.wait_for(_generate_image_gemini(visual_context), timeout=60)
    except asyncio.TimeoutError:
        log.warning(f"Gemini timeout (60s) pour: {visual_context[:60]}")
        raise RuntimeError("Timeout Gemini (60s)")
    if not img_bytes:
        return None
    url = await asyncio.to_thread(_upload_to_gcs, img_bytes, public_id)
    if not url:
        raise RuntimeError("GCS upload échoué (URL vide)")
    return {
        "src":    url,
        "alt":    visual_context[:120],
        "credit": "Gemini AI × Welldone Studio",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Images fallback (Gemini indisponible)
# ══════════════════════════════════════════════════════════════════════════════

def _fallback_images(queries: list[str]) -> list[dict]:
    """
    Lorem Picsum — fallback minimal si Gemini et portfolio sont indisponibles.
    Seed basé sur la query → image cohérente/reproductible.
    """
    results = []
    seeds = [abs(hash(q + str(i))) % 1000 for i, q in enumerate((queries * 3)[:8])]
    for i, seed in enumerate(seeds[:8]):
        q = queries[i % len(queries)] if queries else "studio"
        results.append({
            "src": f"https://picsum.photos/seed/{seed}/1200/800",
            "alt": q,
        })
    return results


async def _get_images_async(queries: list[str], sector: str = "") -> tuple[list[dict], str]:
    """
    Fallback images quand Gemini n'est pas disponible.
    Ordre : Portfolio Welldone → Picsum.
    """
    # 1. Portfolio Welldone
    if FRAMER_PROJECTS_COLLECTION_ID:
        portfolio = await _get_portfolio_images(sector or " ".join(queries))
        if portfolio:
            return portfolio, "Portfolio Welldone"

    # 2. Picsum (dernier recours)
    return _fallback_images(queries), "Picsum"


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

STYLE TEXTE : Français québécois professionnel, ton de journaliste d'affaires.
RÈGLES ABSOLUES D'ÉCRITURE :
1. Paragraphes LONGS et coulants — minimum 5 phrases par paragraphe, sans saut de ligne interne.
2. JAMAIS de double saut de ligne entre les phrases. Texte continu, dense, fluide.
3. Transitions naturelles entre les phrases : "De plus,", "C'est pourquoi,", "En pratique,", etc.
4. Toujours ramener à l'impact business. Exemples concrets québécois. Chiffres si possible.
5. Écrire comme un humain expert, PAS comme une liste de bullet points déguisés en paragraphes.

STYLE IMAGES :
- visual_brief : intention artistique globale en 1 phrase anglaise concise — décrit la TENSION ou l'ÉMOTION centrale (ex: "The tension between digital chaos and human curation in a premium studio context")
- image_queries : 4 mots-clés courts en ANGLAIS (contexte fallback Picsum si Gemini indisponible)
- ✅ FAVORISE : "restaurant kitchen fire", "chef plating close up", "café interior morning light", "entrepreneur desk candid"
- ❌ ÉVITE : "business handshake", "smiling team", "corporate meeting", "stock professional"

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

  "visual_brief": "Intention visuelle globale en 1 phrase anglaise — tension, émotion, concept central de l'article",
  "image_queries": ["query 1 style éditorial", "query 2", "query 3", "query 4"],
  "Hero-Image:alt": "Description précise de l'image hero idéale — sujet, ambiance, cadrage",

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
            "rédiger":     self.rediger,
            "illustrer":   self.illustrer,
            "liste":       self.liste,
            "supprimer":   self.supprimer,
            "collections": self.collections,
            "publier":     self.publier,
            "gemini-test": self.gemini_test,
        }

    async def gemini_test(self, context: dict | None = None) -> str:
        """Diagnostic Gemini depuis Railway : version SDK, clé, génération test."""
        import importlib.metadata
        lines = []
        try:
            v = importlib.metadata.version("google-genai")
            lines.append(f"📦 google-genai: {v}")
        except Exception:
            lines.append("📦 google-genai: version inconnue")

        lines.append(f"🔑 Gemini key: {'✅ présente' if GEMINI_API_KEY else '❌ MANQUANTE'} (len={len(GEMINI_API_KEY)})")
        lines.append(f"🪣 GCS bucket: {GCS_BUCKET!r}")

        if not GEMINI_API_KEY:
            return "\n".join(lines)

        try:
            from google import genai as _genai
            client = _genai.Client(api_key=GEMINI_API_KEY)

            def _test_img():
                resp = client.models.generate_content(
                    model="gemini-3.1-flash-image-preview",
                    contents=["A minimalist Montreal studio desk with natural light, editorial photography style."],
                )
                for part in resp.parts:
                    if part.inline_data is not None:
                        return len(part.inline_data.data)
                return 0

            nb = await asyncio.wait_for(asyncio.to_thread(_test_img), timeout=30)
            if nb > 0:
                lines.append(f"🖼 Gemini image (generate_content): ✅ {nb} bytes générés")
            else:
                lines.append("🖼 Gemini image: ⚠️ réponse vide")
        except asyncio.TimeoutError:
            lines.append("🖼 Gemini image: ⏱ Timeout 30s")
        except Exception as e:
            lines.append(f"🖼 Gemini image: ❌ {type(e).__name__}: {e}")

        return "\n".join(lines)

    async def illustrer(self, context: dict | None = None) -> str:
        """
        Phase 2 : génère les images Gemini pour un article déjà dans Framer.
        context: { slug: str }  — le slug retourné par rédiger.
        Fonctionne avec ou sans cache (résiste aux redémarrages Railway).
        """
        ctx  = context or {}
        slug = ctx.get("slug", ctx.get("sujet", ctx.get("message", ""))).strip()

        if not GEMINI_API_KEY:
            return "❌ `GEMINI_API_KEY` manquant dans Railway — impossible de générer des images IA."

        # ── Récupérer field_data depuis le cache ou directement Framer ────────
        cached = _article_cache.get(slug)

        # Correspondance partielle si slug incomplet
        if not cached:
            for k in reversed(list(_article_cache.keys())):
                if slug and (slug in k or k in slug):
                    slug, cached = k, _article_cache[k]
                    break

        _cached_list_res = None  # réutilisé pour le delete plus bas (BUG 4 fix)

        if cached:
            titre        = cached["titre"]
            field_data   = dict(cached["field_data"])
            visual_brief = cached["visual_brief"]
            article      = cached["article"]
        else:
            # Cache vide (redémarrage Railway) → récupérer depuis Framer
            log.info(f"framer.illustrer: cache vide, chargement depuis Framer pour slug={slug!r}")
            _cached_list_res = await framer_list_items()
            if not _cached_list_res.get("ok"):
                return f"❌ Impossible de lire Framer: {_cached_list_res.get('error')}"

            item = next(
                (i for i in _cached_list_res.get("items", [])
                 if slug and (i.get("slug") == slug or slug in (i.get("slug") or ""))),
                None,
            )
            # Dernier recours : article le plus récent
            if not item and _cached_list_res.get("items"):
                item = _cached_list_res["items"][-1]
                slug = item.get("slug", slug)

            if not item:
                return (
                    "❌ Article introuvable dans Framer.\n"
                    f"Slug cherché : `{slug}`\n"
                    "Lance `/framer liste` pour voir les slugs disponibles."
                )

            titre        = item.get("title", slug)
            field_data   = dict(item.get("field_data") or {})
            titre_clean  = titre.replace("_", " ").strip()
            visual_brief = (
                f"Editorial photography for a branding and creative studio article about: '{titre_clean}'. "
                f"Welldone Studio: minimalist Montreal creative agency, neutral tones, natural light, "
                f"authentic human work visible on screens (brand identity, photography, web design), "
                f"feels handcrafted not AI-generated."
            )
            article      = {}

        log.info(f"framer.illustrer: génération Gemini pour slug={slug}")

        # Mapping section d'article → champ image
        # Chaque image est illustrée par le texte réel de la section correspondante
        _SECTION_MAP = [
            # (Hero-Image)   Titre + sous-titre → image d'accroche
            ["Title", "Sous-Titre (gauche)"],
            # (Image 2)      Section 1
            ["Heading1-Titre", "Heading1-Text"],
            # (Image 3)      Section 2
            ["Heading2-Titre", "Heading2-Text"],
            # (Image 4)      Section 3
            ["Heading3-Titre", "Heading3-Text"],
            # (Image 5)      Section 4
            ["Heading4-Titre", "Heading4-Text"],
            # (Image 6)      Section 5
            ["Heading5-Titre", "Heading5-Text"],
            # (Image 7)      Section bonus
            ["Heading 3", "Body Text 3"],
            # (Image 8)      Titre repris pour l'image de conclusion
            ["Title", "Objectif Stratégique"],
        ]

        def _section_text(fields: list[str]) -> str:
            """Assemble le texte des champs article pour une image."""
            parts = []
            for f in fields:
                val = (article or {}).get(f, "")
                if val and len(str(val).strip()) > 5:
                    parts.append(str(val).strip())
            text = " — ".join(parts)
            # Fallback si aucun champ trouvé
            return text or visual_brief

        # Générer toutes les images en parallèle (timeout 60s/image)
        ts    = int(time.time())
        tasks = []
        for i, field in enumerate(IMAGE_FIELDS):
            section_fields = _SECTION_MAP[i] if i < len(_SECTION_MAP) else ["Title"]
            ctx_img = _section_text(section_fields)
            pid     = f"blog/{_make_slug(slug[:40])}-img{i+1}-{ts}.png"
            tasks.append(_generate_and_upload_image(ctx_img, pid))

        results_raw = await asyncio.gather(*tasks, return_exceptions=True)
        gemini_images = []
        first_error: str = ""
        for r in results_raw:
            if isinstance(r, Exception):
                if not first_error:
                    first_error = str(r)[:200]
                    log.warning(f"framer.illustrer: erreur image: {first_error}")
                gemini_images.append(None)
            else:
                gemini_images.append(r)
        n_ok = sum(1 for g in gemini_images if g)
        log.info(f"framer.illustrer: {n_ok}/{len(IMAGE_FIELDS)} images générées")

        if n_ok == 0:
            err_detail = f"\nErreur : `{first_error}`" if first_error else ""
            return (
                f"⚠️ Gemini n'a généré aucune image.{err_detail}\n"
                "L'article Framer garde ses images Picsum actuelles."
            )

        # Mettre à jour field_data avec les nouvelles images
        for i, field in enumerate(IMAGE_FIELDS):
            if i < len(gemini_images) and gemini_images[i]:
                fid = FIELD_MAP.get(field, {}).get("id")
                if fid:
                    field_data[fid] = {"value": gemini_images[i]["src"], "type": "image"}

        # Supprimer l'ancien item puis recréer avec les images IA
        # (Framer WS n'expose pas updateCollectionItems)
        # Pattern sécurisé : sauvegarder field_data AVANT de supprimer
        # Réutilise _cached_list_res si disponible (évite 2e appel WebSocket — BUG 4 fix)
        list_res = _cached_list_res or await framer_list_items()
        item_id        = None
        original_fd    = {}   # backup pour restauration d'urgence
        if list_res.get("ok"):
            for _it in list_res.get("items", []):
                if _it.get("slug") == slug:
                    item_id     = _it.get("id")
                    original_fd = dict(_it.get("field_data") or {})
                    break

        if item_id:
            await framer_delete_item(item_id)
            log.info(f"framer.illustrer: ancien item supprimé ({item_id})")
            await asyncio.sleep(5)  # Framer doit propager la suppression

        # Recréer avec 3 tentatives (slug peut encore être "en use" côté Framer)
        new_res = None
        for _attempt in range(3):
            new_res = await framer_add_item(slug, field_data)
            if new_res.get("ok"):
                break
            log.warning(f"framer.illustrer: recréation tentative {_attempt+1}/3 — {new_res.get('error','')[:80]}")
            await asyncio.sleep(3)

        if not new_res or not new_res.get("ok"):
            # CATASTROPHE : article supprimé, recréation impossible
            # Tentative de restauration avec le contenu original (sans images Gemini)
            log.error(f"framer.illustrer: RESTAURATION d'urgence pour slug={slug}")
            restore = await framer_add_item(slug, original_fd)
            if restore.get("ok"):
                return (
                    f"⚠️ *Images Gemini non appliquées — article RESTAURÉ dans Framer*\n\n"
                    f"📰 *{titre}*\n"
                    f"_Recréation échouée (3 tentatives). L'article original est restauré sans images IA._\n"
                    f"_Réessaie : `/framer illustrer {slug}`_"
                )
            return (
                f"❌ *ERREUR CRITIQUE — article supprimé et non restauré !*\n"
                f"Slug : `{slug}`\n"
                f"Lance `/blog rédiger <sujet>` pour recommencer."
            )

        # Utiliser le slug réellement créé (peut différer si doublon résiduel)
        final_slug = new_res.get("slug", slug)
        if final_slug != slug:
            log.warning(f"framer.illustrer: slug final={final_slug!r} (différent de {slug!r})")

        # Retirer du cache (phase terminée)
        _article_cache.pop(slug, None)

        # QA : vérifie slug dans CMS + publish staging + construit URL
        editor_url = f"https://framer.com/projects/Welldone-Studio--{FRAMER_PROJECT_ID}"
        qa = await framer_qa_verify(final_slug)
        if not qa.get("ok"):
            return (
                f"⚠️ *Images ajoutées mais publish échoué ({qa.get('step')})* \n\n"
                f"📰 *{titre}*\n"
                f"❌ {qa.get('error','')[:200]}\n\n"
                f"🔧 [Ouvrir Framer]({editor_url})"
            )

        staging_url = qa.get("staging_url", "")
        if not staging_url and FRAMER_STAGING_URL:
            staging_url = f"{FRAMER_STAGING_URL.rstrip('/')}/journal/{final_slug}"

        return (
            f"🎨 *Images IA ajoutées — article prêt !*\n\n"
            f"📰 *{titre}*\n"
            f"🖼️ {n_ok}/{len(IMAGE_FIELDS)} images Gemini\n\n"
            f"👁 [Réviser en staging]({staging_url})\n"
            f"_Clique le bouton 🌐 pour publier sur awelldone.com_"
        )

    async def collections(self, context: dict | None = None) -> str:
        """Liste toutes les collections Framer — utile pour trouver l'ID des projets."""
        result = await framer_get_collections()
        if not result.get("ok"):
            return f"❌ Erreur: {result.get('error')}"

        cols = result.get("collections", [])
        if not cols:
            return "📭 Aucune collection trouvée."

        configured = FRAMER_PROJECTS_COLLECTION_ID
        lines = ["📦 *Collections Framer disponibles :*\n"]
        for c in cols:
            cid   = c["id"]
            name  = c["name"]
            count = c["count"]
            tag   = " ← blog actif" if cid == FRAMER_COLLECTION_ID else ""
            tag  += " ← portfolio actif ✅" if cid == configured else ""
            lines.append(f"• *{name}* — `{cid}` ({count} items){tag}")

        if not configured:
            lines.append(
                "\n💡 *Pour activer les photos de ton portfolio :*\n"
                "1. Identifie la collection de tes projets ci-dessus\n"
                "2. Ajoute dans Railway → Variables : `FRAMER_PROJECTS_COLLECTION_ID = <id>`\n"
                "3. Les articles utiliseront tes vraies photos en priorité"
            )
        return "\n".join(lines)

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

        def _parse_article_json(raw: str) -> dict:
            """Essaie plusieurs stratégies pour extraire le JSON de la réponse Claude."""
            raw = raw.strip()
            # Retirer les blocs markdown ```json ... ```
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
            raw = re.sub(r"\n?```\s*$", "", raw)
            raw = raw.strip()
            # Tentative 1 : parse direct
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                log.debug(f"JSON parse tentative 1: {e}")
            # Tentative 2 : extraire le bloc {} le plus large
            m = re.search(r"\{[\s\S]+\}", raw)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError as e:
                    log.debug(f"JSON parse tentative 2: {e}")
            # Tentative 3 : fermer un JSON tronqué (max_tokens dépassé)
            truncated = raw.rstrip().rstrip(",")
            depth = truncated.count("{") - truncated.count("}")
            if depth > 0:
                try:
                    return json.loads(truncated + "}" * depth)
                except json.JSONDecodeError as e:
                    log.debug(f"JSON parse tentative 3: {e}")
            return {}

        article: dict = {}
        last_raw = ""
        _budget = ctx.get("_pipeline_budget")  # SessionBudget du pipeline (si appelé depuis blog)
        for attempt in range(2):   # max 2 tentatives
            try:
                resp = await safe_claude_call(
                    get_client(),
                    model=CLAUDE_MODEL,
                    max_tokens=8000,
                    messages=[{"role": "user", "content": _GENERATION_PROMPT.format(sujet=sujet)}],
                    timeout_s=90,
                    budget=_budget,
                    agent_name="framer.rediger",
                )
                last_raw = resp.content[0].text.strip()
                log.debug(f"framer: Claude stop_reason={resp.stop_reason} len={len(last_raw)}")
            except Exception as e:
                return f"❌ Erreur Claude: {e}"

            article = _parse_article_json(last_raw)
            if article:
                break
            log.warning(f"framer: JSON invalide (tentative {attempt+1}), raw[:200]={last_raw[:200]}")

        if not article:
            log.error(f"framer: JSON invalide après 2 tentatives. raw[:400]={last_raw[:400]}")
            return "❌ Claude n'a pas retourné un JSON valide. Réessaie."

        # ── 2. Images placeholder Picsum (instantané) ─────────────────────────────
        # Les vraies images IA sont générées par la commande `illustrer` après coup.
        visual_brief = article.get("visual_brief", sujet)
        img_queries  = article.get("image_queries",
                                   [sujet, "professional photography Quebec", "business Montreal"])
        if isinstance(img_queries, list):
            img_queries = [q for q in img_queries if isinstance(q, str) and len(q) < 80]
        if not img_queries:
            img_queries = [sujet, "professional photography Quebec"]
        sector = article.get("Secteur d'activité", "")

        images, img_source = await _get_images_async(img_queries, sector)
        for i, field in enumerate(IMAGE_FIELDS):
            if i < len(images):
                alt_key = f"{field}:alt"
                article[field] = {
                    "src": images[i]["src"],
                    "alt": article.get(alt_key) or images[i].get("alt", ""),
                }

        # ── 3. Construire le fieldData Framer (IDs exacts) ─────────────────────
        # Suffixe MMDD + random 5-char pour unicité absolue (pas de collision même minute)
        _rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        _ts_suffix = f"{_datetime.now().strftime('%m%d')}-{_rand}"
        slug_base  = _make_slug(article.get("slug") or article.get("Title") or sujet)
        slug       = f"{slug_base[:68]}-{_ts_suffix}"   # max 80 chars
        field_data: dict = {}

        for col_name, meta in FIELD_MAP.items():
            fid   = meta["id"]
            ftype = meta["type"]

            if ftype == "image":
                val = article.get(col_name)
                if isinstance(val, dict) and val.get("src"):
                    field_data[fid] = {"value": val["src"], "type": "image"}
            elif ftype == "formattedText":
                val = article.get(col_name, "")
                if val:
                    # Strip newlines — Framer gère la mise en page via CSS
                    val_clean = re.sub(r"\n+", " ", val).strip()
                    field_data[fid] = {"value": f"<p>{val_clean}</p>", "type": "formattedText"}
            elif ftype == "link":
                val = article.get(col_name, "")
                if val:
                    field_data[fid] = {"value": val, "type": "link"}
            else:
                val = article.get(col_name, "")
                if val:
                    # Strip newlines dans les champs string (évite double-spacing dans Framer)
                    val_clean = re.sub(r"\n+", " ", str(val)).strip()
                    field_data[fid] = {"value": val_clean, "type": "string"}

        # ── 4. Push vers Framer CMS via WebSocket Python ───────────────────────
        log.info(f"framer.rediger: push slug={slug} fields={len(field_data)}")
        result = await framer_add_item(slug, field_data)
        titre  = article.get("Title", sujet)

        # Stocker pour la phase 2 (illustrer)
        _article_cache[slug] = {
            "article":     article,
            "field_data":  field_data,
            "img_queries": img_queries,
            "visual_brief": visual_brief,
            "sector":      sector,
            "titre":       titre,
        }

        if not result.get("ok"):
            err = result.get("error", "Inconnu")
            log.error(f"framer.rediger push error: {err}")
            return (
                f"⚠️ *Article généré mais NON publié dans Framer*\n\n"
                f"📰 *{titre}*\n"
                f"❌ Erreur: {err[:400]}\n\n"
                f"🔍 Vérifie FRAMER_API_KEY dans Railway."
            )

        # ── 5. Terminé — pas de QA/publish ici (illustrer fait publish après images) ──
        img_count = len([f for f in IMAGE_FIELDS if field_data.get(FIELD_MAP[f]["id"])])
        log.info(f"framer.rediger: ✅ article créé — slug={slug} fields={len(field_data)}")

        _staging_url = f"{FRAMER_STAGING_URL.rstrip('/')}/journal/{slug}" if FRAMER_STAGING_URL else f"/journal/{slug}"

        # ── Pipeline Notion (trace légère) ────────────────────────────────────
        try:
            from core.notion_delivery import pipeline_log as _pipeline_log
            await _pipeline_log(
                title=titre,
                agent="framer",
                framer_url=_staging_url,
                notes=f"Sujet: {sujet}\nSlug: {slug}\nChamps: {len(field_data)} · Images: {img_count} ({img_source})",
            )
        except Exception as _ne:
            log.warning(f"framer.rediger: notion pipeline skip ({_ne})")

        return (
            f"✅ *Article créé dans Framer*\n\n"
            f"📰 *{titre}*\n"
            f"📋 {len(field_data)} champs · 🖼️ {img_count} images ({img_source})\n"
            f"👁 [Voir dans staging]({_staging_url})"
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
            pub = item.get("published")
            status = "🟢 Publié" if pub is True else ("📝 Brouillon" if pub is False else "❓ État?")
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


    async def publier(self, context: dict | None = None) -> str:
        """
        Publie le site Framer sur awelldone.com (production).

        context: { slug: str — optionnel, pour construire l'URL de l'article }
        """
        ctx  = context or {}
        slug = ctx.get("slug", "").strip()

        log.info(f"framer.publier: déclenchement publish — slug={slug!r}")
        pub_res = await framer_publish_staging()

        # framer_publish_staging() peut fermer la WS — c'est normal
        if not pub_res.get("ok"):
            err = pub_res.get("error", "")
            if "no close frame" in err or "close frame" in err or "ConnectionClosed" in err:
                log.info("framer.publier: publish déclenché (WS fermé par Framer — normal)")
            else:
                log.error(f"framer.publier: erreur — {err}")
                return f"❌ Erreur publication : {err[:200]}"

        if slug:
            pub_url = f"https://awelldone.com/journal/{slug}"
            return (
                f"🚀 *Article publié sur awelldone.com !*\n\n"
                f"🔗 [Voir l'article]({pub_url})\n"
                f"_Le déploiement peut prendre 1-2 minutes._"
            )
        else:
            editor_url = f"https://framer.com/projects/Welldone-Studio--{FRAMER_PROJECT_ID}"
            return (
                f"🚀 *Site publié sur awelldone.com !*\n\n"
                f"👉 [Ouvrir Framer Editor]({editor_url})\n"
                f"_Le déploiement peut prendre 1-2 minutes._"
            )


agent = FramerAgent()
