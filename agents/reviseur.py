"""
agents/reviseur.py — Agent de révision chirurgicale du contenu CMS Framer.

Toutes les collections (journal, projets, etc.) sont supportées.
Chirurgical : génère uniquement les champs modifiés, jamais toute la page.
Itératif    : guide de structure validé par JP, recommandations numérotées.

Signal NEEDS_COLLECTION → bot/telegram.py affiche un clavier de sélection.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime

from agents._base import BaseAgent
from agents.framer import (
    FIELD_MAP, IMAGE_FIELDS,
    FramerClient, FRAMER_API_KEY, FRAMER_COLLECTION_ID,
    FRAMER_STAGING_URL,
    _framer_op, framer_get_collections,
)
from core.brain import get_client
from core.guardrails import safe_claude_call, SessionBudget
from config import CLAUDE_MODEL

log = logging.getLogger(__name__)

# ── Signal (même pattern que QBO) ────────────────────────────────────────────
NEEDS_COLLECTION = "__REVISEUR_NEEDS_COLLECTION__"

# ── Persistent state files (project root) ─────────────────────────────────────
_BASE         = os.path.dirname(os.path.dirname(__file__))
_GUIDE_PATH   = os.path.join(_BASE, "reviseur_guide.json")
_PENDING_PATH = os.path.join(_BASE, "reviseur_pending.json")

# ── Session state (user_id → pending callback data) ──────────────────────────
_session: dict[int, dict] = {}


def store_session(user_id: int, command: str, ctx: dict, collections: list) -> None:
    _session[user_id] = {"command": command, "ctx": dict(ctx), "collections": collections}


def get_session(user_id: int) -> dict | None:
    return _session.get(user_id)


def clear_session(user_id: int) -> None:
    _session.pop(user_id, None)


def get_collection_keyboard_data(user_id: int) -> list[dict]:
    """Returns list of {id, name} for building the Telegram keyboard."""
    sess = _session.get(user_id)
    return sess.get("collections", []) if sess else []


# ── Guide / pending file helpers ──────────────────────────────────────────────

def _load_guide() -> dict:
    try:
        with open(_GUIDE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_guide(data: dict) -> None:
    with open(_GUIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_pending() -> dict:
    try:
        with open(_PENDING_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending(data: dict) -> None:
    with open(_PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Framer helpers (collection-agnostic) ──────────────────────────────────────

async def _list_collection(collection_id: str) -> dict:
    """List all items in any Framer collection."""
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.get_items(collection_id)

    res = await _framer_op(_do())
    if not res["ok"]:
        return res

    raw   = res["data"] or []
    items = []
    for item in (raw if isinstance(raw, list) else []):
        fd    = item.get("fieldData") or {}
        title = item.get("slug", "(sans titre)")
        for fval in fd.values():
            if isinstance(fval, dict) and fval.get("type") == "string" and fval.get("value"):
                title = str(fval["value"])[:60]
                break
        items.append({
            "id":         item.get("id"),
            "slug":       item.get("slug"),
            "title":      title,
            "field_data": fd,
        })
    return {"ok": True, "items": items, "count": len(items)}


async def _add_to_collection(collection_id: str, slug: str, field_data: dict) -> dict:
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.add_items(collection_id, [{"slug": slug, "fieldData": field_data}])
    return await _framer_op(_do())


async def _remove_from_collection(collection_id: str, item_id: str) -> dict:
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.remove_items(collection_id, [{"id": item_id}])
    return await _framer_op(_do())


async def _publish_staging() -> dict:
    async def _do():
        async with FramerClient(FRAMER_API_KEY) as c:
            return await c.publish_site()
    res = await _framer_op(_do())
    if not res.get("ok"):
        err = res.get("error", "")
        if any(x in err for x in ("no close frame", "ConnectionClosed", "close frame")):
            return {"ok": True, "note": "publish déclenché (WS fermé par Framer — normal)"}
    return res


# ── Field map helpers ─────────────────────────────────────────────────────────

def _get_field_map(collection_id: str) -> dict:
    """Static FIELD_MAP for the journal collection; empty dict for others."""
    return FIELD_MAP if collection_id == FRAMER_COLLECTION_ID else {}


def _build_dynamic_field_map(items: list) -> dict:
    """Discover field IDs and types from existing items (for unknown collections)."""
    if not items:
        return {}
    sample_fd = items[0].get("field_data", {})
    result = {}
    for fid, fval in sample_fd.items():
        ftype = fval.get("type", "string") if isinstance(fval, dict) else "string"
        result[fid] = {"id": fid, "type": ftype}
    return result


# ── Article parsing helpers ───────────────────────────────────────────────────

def _field_data_to_readable(field_data: dict, field_map: dict) -> dict:
    """
    Converts Framer field_data (opaque IDs) to human-readable names.
    Images → '[IMAGE PRÉSENTE]' to save tokens.
    """
    if not field_map:
        readable = {}
        for fid, fval in field_data.items():
            if not isinstance(fval, dict):
                continue
            ftype = fval.get("type", "string")
            val   = fval.get("value")
            if not val:
                continue
            if ftype == "image":
                readable[fid] = "[IMAGE PRÉSENTE]"
            elif ftype == "formattedText":
                readable[fid] = re.sub(r"<[^>]+>", "", str(val)).strip()
            else:
                readable[fid] = str(val)
        return readable

    reverse = {meta["id"]: name for name, meta in field_map.items()}
    readable = {}
    for fid, fval in field_data.items():
        name = reverse.get(fid)
        if not name or not isinstance(fval, dict):
            continue
        ftype = fval.get("type", "string")
        val   = fval.get("value")
        if not val:
            continue
        if ftype == "image" or name in IMAGE_FIELDS:
            readable[name] = "[IMAGE PRÉSENTE]"
        elif ftype == "formattedText":
            readable[name] = re.sub(r"<[^>]+>", "", str(val)).strip()
        else:
            readable[name] = str(val)
    return readable


def _build_corpus_summary(items: list, field_map: dict) -> str:
    """
    Compact statistical summary of a collection corpus (avoids passing full text to Claude).
    """
    if not items:
        return "Aucun item trouvé dans la collection."

    field_counts:  dict[str, int]       = {}
    field_lengths: dict[str, list[int]] = {}

    for item in items:
        readable = _field_data_to_readable(item.get("field_data", {}), field_map)
        for fname, fval in readable.items():
            if fval == "[IMAGE PRÉSENTE]":
                continue
            field_counts[fname] = field_counts.get(fname, 0) + 1
            words = len(str(fval).split())
            field_lengths.setdefault(fname, []).append(words)

    n     = len(items)
    lines = [f"{n} items analysés.\n", "Champs texte — présence et longueur moyenne :"]
    for fname in sorted(field_counts):
        count    = field_counts[fname]
        pct      = round(count / n * 100)
        lengths  = field_lengths.get(fname, [0])
        avg_w    = round(sum(lengths) / max(len(lengths), 1))
        lines.append(f"  {fname}: {count}/{n} ({pct}%) — ~{avg_w} mots")

    return "\n".join(lines)


def _parse_json_safe(raw: str) -> dict | list | None:
    """3-strategy JSON parser (same approach as framer.py)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    starts = [raw.find("["), raw.find("{")]
    ends   = [raw.rfind("]"), raw.rfind("}")]
    start  = min(s for s in starts if s != -1) if any(s != -1 for s in starts) else -1
    end    = max(ends)
    if start != -1 and start < end:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


# ── Patch helper ──────────────────────────────────────────────────────────────

def _get_protected_fields(guide: dict) -> set[str]:
    """Returns the set of field names marked as 'protégé' in the guide."""
    return {
        fname
        for fname, rules in guide.get("field_rules", {}).items()
        if rules.get("category") == "protégé"
    }


def _split_readable(readable: dict, protected: set[str]) -> tuple[dict, dict]:
    """
    Splits a readable field dict into (style_fields, factual_context).
    style_fields    → sent to Claude for review/editing
    factual_context → sent as read-only context, never modified
    """
    style   = {k: v for k, v in readable.items() if k not in protected and v != "[IMAGE PRÉSENTE]"}
    factual = {k: v for k, v in readable.items() if k in protected and v != "[IMAGE PRÉSENTE]"}
    return style, factual


def _apply_patch(existing_fd: dict, partial_json: dict, field_map: dict,
                 protected: set[str] | None = None) -> dict:
    """
    Merges Claude's partial JSON (field_name → new_value) into existing Framer field_data.
    Returns updated field_data dict keyed by Framer field IDs.
    Protected fields are silently skipped (firewall).
    """
    protected = protected or set()
    result    = dict(existing_fd)

    if field_map:
        for fname, new_val in partial_json.items():
            if fname in protected:
                log.warning(f"reviseur patch: champ protégé '{fname}' — modification refusée")
                continue
            meta = field_map.get(fname)
            if not meta:
                log.warning(f"reviseur patch: champ inconnu '{fname}' — ignoré")
                continue
            fid   = meta["id"]
            ftype = meta["type"]
            if ftype == "image":
                continue  # Images gérées séparément
            clean = re.sub(r"\n+", " ", str(new_val)).strip()
            if ftype == "formattedText":
                result[fid] = {"value": f"<p>{clean}</p>", "type": "formattedText"}
            elif ftype == "link":
                result[fid] = {"value": clean, "type": "link"}
            else:
                result[fid] = {"value": clean, "type": "string"}
    else:
        # Dynamic: field IDs used directly as keys
        for fid, new_val in partial_json.items():
            if fid in protected:
                log.warning(f"reviseur patch: champ protégé '{fid}' — modification refusée")
                continue
            existing_type = "string"
            if fid in existing_fd and isinstance(existing_fd[fid], dict):
                existing_type = existing_fd[fid].get("type", "string")
            if existing_type == "image":
                continue
            clean = re.sub(r"\n+", " ", str(new_val)).strip()
            if existing_type == "formattedText":
                result[fid] = {"value": f"<p>{clean}</p>", "type": "formattedText"}
            elif existing_type == "link":
                result[fid] = {"value": clean, "type": "link"}
            else:
                result[fid] = {"value": clean, "type": "string"}

    return result


# ── Claude system prompts ─────────────────────────────────────────────────────

_SYSTEM_ANALYSER = """Tu es expert en stratégie de contenu pour Welldone Studio (Montréal).
Tu analyses un corpus d'items CMS Framer et proposes un guide de structure éditorial.

Retourne UNIQUEMENT ce JSON (sans markdown autour) :
{
  "collection_name": "Nom lisible de la collection",
  "mode": "seo_blog|factuel",
  "validated": false,
  "narrative_arc": "Arc narratif recommandé (1-2 phrases)",
  "field_rules": {
    "NomChamp": {
      "category": "style|protégé",
      "required": true,
      "notes": "Règle (longueur, SEO, ton, etc.)"
    }
  },
  "style_rules": ["Règle 1", "Règle 2"],
  "tone": "Ton recommandé (1 phrase)",
  "analysis_notes": "Observations clés du corpus (2-3 phrases)"
}

Règles pour le champ `mode` :
- "seo_blog"  → contenu éditorial (blog, articles) — latitude créative, optimisation SEO autorisée
- "factuel"   → contenu factuel (projets, réalisations, portfolio) — les faits DOIVENT être préservés

Règles pour `category` dans field_rules :
- "protégé"  → champ factuel (nom client, lieu, date, résultats, type de mandat, liens) — JAMAIS modifié par le réviseur
- "style"    → champ textuel modifiable (titres éditoriaux, paragraphes, méta-description, FAQ)
- Les champs images sont toujours protégés (ne pas les inclure dans field_rules)

Autres règles :
- Propose uniquement les champs réellement présents dans le corpus
- Sois précis sur les longueurs (mots min/max) pour les champs "style"
- Focus SEO + conversion (style Welldone Studio)
- Québécois francophone professionnel"""

_SYSTEM_REVIEWER = """Tu es éditeur éditorial pour Welldone Studio (Montréal).
Tu compares un item CMS avec le guide de structure validé et retournes des recommandations chirurgicales.

Tu reçois deux sections :
- CONTEXTE FACTUEL (lecture seule) : faits réels de l'item — NE JAMAIS recommander de les modifier
- CONTENU RÉVISABLE : champs "style" que tu peux améliorer

Retourne UNIQUEMENT ce JSON array (sans markdown autour) :
[
  {
    "num": 1,
    "champs": ["NomChamp1"],
    "type": "expand|trim|rewrite|add|remove",
    "current_preview": "Premiers 80 chars du contenu actuel",
    "rationale": "Raison concrète en 1 phrase"
  }
]

Règles absolues :
- MAX 12 recommandations, classées par priorité d'impact (SEO > longueur > ton > structure)
- JAMAIS recommander de régénérer tout l'article
- JAMAIS toucher aux champs images ou aux champs du CONTEXTE FACTUEL
- Si l'item est excellent → retourne {"excellent": true, "note": "..."}
- Chaque recommandation = max 3 champs par item"""

_SYSTEM_EDITOR = """Tu es l'éditeur chirurgical de Welldone Studio.
Tu reçois des recommandations précises et le contenu actuel des champs affectés seulement.
Tu génères le nouveau contenu pour UNIQUEMENT ces champs.

RÈGLE ABSOLUE : Tu peux recevoir un CONTEXTE FACTUEL en lecture seule (nom client, lieu, résultats).
Ces faits doivent APPARAÎTRE dans ton contenu si pertinents, mais jamais être modifiés ou inventés.
Ne jamais fabriquer de chiffres, noms, dates ou résultats qui ne figurent pas dans le contexte fourni.

Style obligatoire : français québécois professionnel, paragraphes denses (≥5 phrases), pas de bullet points déguisés, exemples québécois quand applicable.

Retourne UNIQUEMENT ce JSON (sans markdown autour, aucun autre champ) :
{
  "NomChamp1": "Nouveau contenu...",
  "NomChamp2": "Nouveau contenu..."
}"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class ReviseurAgent(BaseAgent):
    name        = "reviseur"
    description = "Révision chirurgicale du contenu CMS Framer (toutes collections)"

    @property
    def commands(self):
        return {
            "collections": self.cmd_collections,
            "analyser":    self.cmd_analyser,
            "valider":     self.cmd_valider,
            "réviser":     self.cmd_reviser,
            "appliquer":   self.cmd_appliquer,
            "éditer":      self.cmd_editer,
            "liste":       self.cmd_liste,
        }

    # ── Internal: collection picker ───────────────────────────────────────────

    async def _require_collection(self, ctx: dict, command: str) -> str | None:
        """
        Returns the collection_id if provided in ctx.
        Otherwise fetches collections, stores in session, returns NEEDS_COLLECTION.
        Returns None on Framer error.
        """
        col = ctx.get("collection")
        if col:
            return col

        res = await framer_get_collections()
        if not res.get("ok"):
            return None

        collections = res.get("collections", [])
        user_id     = int(ctx.get("_user_id", 0))
        store_session(user_id, command, ctx, collections)
        return NEEDS_COLLECTION

    # ── Commande : collections ────────────────────────────────────────────────

    async def cmd_collections(self, context: dict | None = None) -> str:
        res = await framer_get_collections()
        if not res.get("ok"):
            return f"❌ Erreur Framer : {res.get('error')}"
        cols = res.get("collections", [])
        if not cols:
            return "📭 Aucune collection trouvée."

        guide = _load_guide()
        lines = ["📦 *Collections Framer disponibles :*\n"]
        for c in cols:
            cid       = c["id"]
            name      = c["name"]
            cnt       = c.get("count", "?")
            validated = "✅" if guide.get(cid, {}).get("validated") else "⬜"
            lines.append(f"{validated} *{name}* — `{cid}` ({cnt} items)")
        return "\n".join(lines)

    # ── Commande : analyser ───────────────────────────────────────────────────

    async def cmd_analyser(self, context: dict | None = None) -> str:
        ctx = context or {}
        col = await self._require_collection(ctx, "analyser")
        if col == NEEDS_COLLECTION:
            return NEEDS_COLLECTION
        if col is None:
            return "❌ Impossible de récupérer les collections Framer."

        list_res = await _list_collection(col)
        if not list_res.get("ok"):
            return f"❌ Erreur Framer : {list_res.get('error')}"

        items = list_res.get("items", [])
        if not items:
            return f"📭 Aucun item dans la collection `{col}`."

        field_map = _get_field_map(col) or _build_dynamic_field_map(items)
        corpus    = _build_corpus_summary(items, field_map)

        # Exclure les champs image de la liste envoyée à Claude
        field_names = [k for k, v in field_map.items() if v.get("type") != "image"]

        budget = SessionBudget(limit=12000)
        client = get_client()
        prompt = (
            f"Collection Framer : `{col}` ({len(items)} items)\n\n"
            f"Champs disponibles :\n{json.dumps(field_names, ensure_ascii=False)}\n\n"
            f"Résumé statistique du corpus :\n{corpus}\n\n"
            f"Propose un guide de structure éditorial pour cette collection."
        )

        resp = await safe_claude_call(
            client,
            model=CLAUDE_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM_ANALYSER,
            budget=budget,
            agent_name="reviseur.analyser",
        )

        raw = "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))
        guide_data = _parse_json_safe(raw)
        if not guide_data or not isinstance(guide_data, dict):
            return f"❌ Réponse Claude non parseable.\n\n```\n{raw[:500]}\n```"

        all_guides = _load_guide()
        guide_data["validated"]  = False
        guide_data["created_at"] = datetime.now().isoformat()
        all_guides[col]          = guide_data
        _save_guide(all_guides)

        arc   = guide_data.get("narrative_arc", "")
        notes = guide_data.get("analysis_notes", "")
        tone  = guide_data.get("tone", "")
        rules = guide_data.get("style_rules", [])

        lines = [
            f"📋 *Guide proposé — {guide_data.get('collection_name', col)}*\n",
            f"*Arc narratif :* {arc}",
            f"*Ton :* {tone}",
            f"*Analyse :* {notes}",
        ]
        if rules:
            lines.append("\n*Règles de style :*")
            for r in rules[:5]:
                lines.append(f"  • {r}")
        lines.append(
            f"\n_Guide sauvegardé (non validé). Lance :_\n"
            f"`/reviseur valider --collection {col}`\n"
            f"_ou avec ajustements :_\n"
            f"`/reviseur valider --collection {col} --ajustements \"tes corrections\"`"
        )
        return "\n".join(lines)

    # ── Commande : valider ────────────────────────────────────────────────────

    async def cmd_valider(self, context: dict | None = None) -> str:
        ctx = context or {}
        col = ctx.get("collection")
        if not col:
            return "❌ Précise la collection : `/reviseur valider --collection <id>`"

        all_guides = _load_guide()
        if col not in all_guides:
            return (
                f"❌ Aucun guide pour `{col}`.\n"
                f"Lance d'abord `/reviseur analyser --collection {col}`."
            )

        guide       = all_guides[col]
        ajustements = ctx.get("ajustements", "").strip()

        if ajustements:
            budget = SessionBudget(limit=3000)
            client = get_client()
            prompt = (
                f"Guide actuel :\n{json.dumps(guide, ensure_ascii=False, indent=2)}\n\n"
                f"Ajustements de JP :\n{ajustements}\n\n"
                f"Retourne le guide mis à jour en JSON (même structure, intègre les ajustements)."
            )
            resp = await safe_claude_call(
                client,
                model=CLAUDE_MODEL,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
                system="Tu intègres les ajustements dans un guide JSON éditorial. Retourne uniquement le JSON mis à jour.",
                budget=budget,
                agent_name="reviseur.valider",
            )
            raw     = "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))
            updated = _parse_json_safe(raw)
            if updated and isinstance(updated, dict):
                guide = updated

        guide["validated"]    = True
        guide["validated_at"] = datetime.now().isoformat()
        all_guides[col]       = guide
        _save_guide(all_guides)

        name = guide.get("collection_name", col)
        return (
            f"✅ *Guide validé — {name}*\n\n"
            f"Arc : {guide.get('narrative_arc', '—')}\n"
            f"Ton : {guide.get('tone', '—')}\n\n"
            f"_Lance `/reviseur liste --collection {col}` pour voir les items disponibles._"
        )

    # ── Commande : liste ──────────────────────────────────────────────────────

    async def cmd_liste(self, context: dict | None = None) -> str:
        ctx = context or {}
        col = await self._require_collection(ctx, "liste")
        if col == NEEDS_COLLECTION:
            return NEEDS_COLLECTION
        if col is None:
            return "❌ Impossible de récupérer les collections Framer."

        list_res = await _list_collection(col)
        if not list_res.get("ok"):
            return f"❌ Erreur Framer : {list_res.get('error')}"

        items = list_res.get("items", [])
        if not items:
            return f"📭 Aucun item dans la collection `{col}`."

        pending = _load_pending()
        lines   = [f"📋 *{len(items)} items — `{col}` :*\n"]
        for item in items:
            slug   = item["slug"]
            title  = item["title"]
            key    = f"{col}:{slug}"
            n_recs = len([
                r for r in pending.get(key, {}).get("recommendations", [])
                if not r.get("applied")
            ])
            badge = f" ⚡ {n_recs} recs" if n_recs else ""
            lines.append(f"  • `{slug}` — {title}{badge}")

        return "\n".join(lines)

    # ── Commande : réviser ────────────────────────────────────────────────────

    async def cmd_reviser(self, context: dict | None = None) -> str:
        ctx = context or {}
        col = await self._require_collection(ctx, "réviser")
        if col == NEEDS_COLLECTION:
            return NEEDS_COLLECTION
        if col is None:
            return "❌ Impossible de récupérer les collections Framer."

        slug = ctx.get("slug")
        if not slug:
            return f"❌ Précise le slug : `/reviseur réviser --collection {col} --slug <slug>`"

        all_guides = _load_guide()
        guide      = all_guides.get(col)
        if not guide:
            return (
                f"❌ Aucun guide pour `{col}`.\n"
                f"Lance `/reviseur analyser --collection {col}` d'abord."
            )
        if not guide.get("validated"):
            return (
                f"⚠️ Guide non validé pour `{col}`.\n"
                f"Lance `/reviseur valider --collection {col}` d'abord."
            )

        list_res = await _list_collection(col)
        if not list_res.get("ok"):
            return f"❌ Erreur Framer : {list_res.get('error')}"

        items  = list_res.get("items", [])
        target = next((i for i in items if i["slug"] == slug), None)
        if not target:
            return f"❌ Slug `{slug}` introuvable dans la collection `{col}`."

        field_map = _get_field_map(col) or _build_dynamic_field_map(items)
        readable  = _field_data_to_readable(target["field_data"], field_map)

        protected      = _get_protected_fields(guide)
        style_fields, factual_ctx = _split_readable(readable, protected)

        # Only include style field rules in the guide compact (not protected)
        style_rules_notes = {
            k: v.get("notes", "")
            for k, v in guide.get("field_rules", {}).items()
            if v.get("category") != "protégé"
        }
        guide_compact = (
            f"Mode : {guide.get('mode', 'seo_blog')}\n"
            f"Arc : {guide.get('narrative_arc', '')}\n"
            f"Ton : {guide.get('tone', '')}\n"
            f"Règles : {'; '.join(guide.get('style_rules', []))}\n"
            f"Champs style requis : {json.dumps(style_rules_notes, ensure_ascii=False)}"
        )

        # Inject analytics keywords for seo_blog collections
        analytics_context = ""
        if guide.get("mode", "seo_blog") == "seo_blog":
            try:
                from core.dispatcher import dispatch as _dispatch
                kw_result = await asyncio.wait_for(
                    _dispatch("analytics", "opportunities", {}),
                    timeout=30,
                )
                if kw_result and "❌" not in kw_result:
                    # Extract just the first 400 chars (compact keyword list)
                    kw_lines = [
                        line for line in kw_result.splitlines()
                        if line.strip() and not line.startswith("*") and "pos" in line.lower()
                    ][:8]
                    if kw_lines:
                        analytics_context = (
                            "\n\nMOTS-CLÉS GSC à prioriser (pos 4-20) :\n"
                            + "\n".join(kw_lines)
                            + "\n→ Intègre naturellement ces termes dans tes recommandations si pertinent."
                        )
            except Exception as e:
                log.debug(f"reviseur: analytics skip ({e})")

        budget = SessionBudget(limit=8000)
        client = get_client()

        factual_section = (
            f"\nCONTEXTE FACTUEL (lecture seule — ne jamais recommander de modifier) :\n"
            f"{json.dumps(factual_ctx, ensure_ascii=False, indent=2)}\n"
            if factual_ctx else ""
        )

        prompt = (
            f"Guide de structure validé :\n{guide_compact}{analytics_context}\n\n"
            f"{factual_section}"
            f"CONTENU RÉVISABLE (slug: {slug}) :\n"
            f"{json.dumps(style_fields, ensure_ascii=False, indent=2)}\n\n"
            f"Retourne la liste JSON des recommandations."
        )

        resp = await safe_claude_call(
            client,
            model=CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM_REVIEWER,
            budget=budget,
            agent_name="reviseur.réviser",
        )

        raw    = "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))
        parsed = _parse_json_safe(raw)

        if isinstance(parsed, dict) and parsed.get("excellent"):
            return f"🌟 *Article excellent !*\n\n{parsed.get('note', 'Aucune modification recommandée.')}"

        if not isinstance(parsed, list):
            return f"❌ Réponse Claude non parseable.\n\n```\n{raw[:400]}\n```"

        if not parsed:
            return "🌟 *Aucune recommandation — item conforme au guide !*"

        all_pending = _load_pending()
        key         = f"{col}:{slug}"
        all_pending[key] = {
            "collection":      col,
            "slug":            slug,
            "item_id":         target["id"],
            "title":           target["title"],
            "fetched_at":      datetime.now().isoformat(),
            "recommendations": [
                {**r, "applied": False}
                for r in parsed
                if isinstance(r, dict) and "num" in r
            ],
        }
        _save_pending(all_pending)

        lines = [f"🔍 *{len(parsed)} recommandations — {target['title']}*\n"]
        for r in parsed:
            if not isinstance(r, dict):
                continue
            num     = r.get("num", "?")
            champs  = ", ".join(r.get("champs", []))
            rtype   = r.get("type", "").upper()
            preview = r.get("current_preview", "")
            reason  = r.get("rationale", "")
            lines.append(
                f"*{num}.* `{champs}` — {rtype}\n"
                f"   _{preview[:70]}_\n"
                f"   → {reason}\n"
            )
        lines.append(
            f"_Lance `/reviseur appliquer --collection {col} --slug {slug} --numeros \"1, 3\"` (ou \"toutes\")_"
        )
        return "\n".join(lines)

    # ── Commande : appliquer ──────────────────────────────────────────────────

    async def cmd_appliquer(self, context: dict | None = None) -> str:
        ctx     = context or {}
        col     = await self._require_collection(ctx, "appliquer")
        if col == NEEDS_COLLECTION:
            return NEEDS_COLLECTION
        if col is None:
            return "❌ Impossible de récupérer les collections Framer."

        slug    = ctx.get("slug")
        numeros = ctx.get("numeros", "")

        if not slug:
            return (
                f"❌ Précise le slug :\n"
                f"`/reviseur appliquer --collection {col} --slug <slug> --numeros \"1,3\"`"
            )

        key         = f"{col}:{slug}"
        all_pending = _load_pending()
        entry       = all_pending.get(key)

        if not entry:
            return (
                f"❌ Aucune recommandation en attente pour `{slug}`.\n"
                f"Lance d'abord `/reviseur réviser --collection {col} --slug {slug}`."
            )

        recs = entry.get("recommendations", [])

        # Parse numéros
        if numeros.strip().lower() in ("toutes", "all", "tous"):
            selected_nums = {r["num"] for r in recs if not r.get("applied")}
        else:
            selected_nums = set()
            for part in re.split(r"[,\s]+", numeros):
                if part.strip().isdigit():
                    selected_nums.add(int(part.strip()))

        selected_recs = [
            r for r in recs if r.get("num") in selected_nums and not r.get("applied")
        ]
        if not selected_recs:
            return f"❌ Aucune recommandation valide pour les numéros : {numeros}"

        # Fetch live item data (never use stale snapshot for merge)
        list_res = await _list_collection(col)
        if not list_res.get("ok"):
            return f"❌ Erreur Framer (fetch live) : {list_res.get('error')}"

        items  = list_res.get("items", [])
        target = next((i for i in items if i["slug"] == slug), None)
        if not target:
            return f"❌ Slug `{slug}` introuvable (supprimé entre-temps ?)."

        field_map = _get_field_map(col) or _build_dynamic_field_map(items)
        readable  = _field_data_to_readable(target["field_data"], field_map)

        all_guides = _load_guide()
        guide      = all_guides.get(col, {})
        protected  = _get_protected_fields(guide)

        # Filter out any recs that target protected fields (safety net)
        safe_recs = [
            r for r in selected_recs
            if not any(c in protected for c in r.get("champs", []))
        ]
        blocked = [r for r in selected_recs if r not in safe_recs]
        if blocked:
            blocked_info = ", ".join(
                f"#{r['num']} ({', '.join(r.get('champs', []))})" for r in blocked
            )
            log.warning(f"reviseur appliquer: recs bloquées (champs protégés) : {blocked_info}")
        if not safe_recs:
            blocked_nums = ", ".join("#" + str(r["num"]) for r in blocked)
            return (
                f"⛔ Toutes les recommandations sélectionnées touchent des champs protégés.\n"
                f"Bloquées : {blocked_nums}"
            )

        # Build patch context — only affected style fields for Claude
        affected = set()
        for r in safe_recs:
            affected.update(r.get("champs", []))
        current_affected = {
            k: v for k, v in readable.items()
            if k in affected and v != "[IMAGE PRÉSENTE]" and k not in protected
        }

        # Factual context (read-only anchor for the editor)
        _, factual_ctx = _split_readable(readable, protected)

        guide_compact = f"Ton : {guide.get('tone', '')}. {' '.join(guide.get('style_rules', []))}"

        recs_text = "\n".join(
            f"Rec #{r['num']} — {', '.join(r.get('champs', []))} — "
            f"{r.get('type','').upper()}: {r.get('rationale','')}"
            for r in safe_recs
        )

        factual_section = (
            f"CONTEXTE FACTUEL (ancre — utilise ces faits si pertinents, ne les invente JAMAIS) :\n"
            f"{json.dumps(factual_ctx, ensure_ascii=False, indent=2)}\n\n"
            if factual_ctx else ""
        )

        budget = SessionBudget(limit=6000)
        client = get_client()
        prompt = (
            f"Guide de style : {guide_compact}\n\n"
            f"{factual_section}"
            f"Recommandations à appliquer :\n{recs_text}\n\n"
            f"Contenu actuel des champs affectés seulement :\n"
            f"{json.dumps(current_affected, ensure_ascii=False, indent=2)}\n\n"
            f"Retourne le JSON avec UNIQUEMENT les champs modifiés."
        )

        resp = await safe_claude_call(
            client,
            model=CLAUDE_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM_EDITOR,
            budget=budget,
            agent_name="reviseur.appliquer",
        )

        raw   = "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))
        patch = _parse_json_safe(raw)
        if not patch or not isinstance(patch, dict):
            return f"❌ Réponse Claude non parseable.\n\n```\n{raw[:400]}\n```"

        # Snapshot before delete (disaster recovery)
        all_pending[key]["last_snapshot"] = {
            "slug":       slug,
            "item_id":    target["id"],
            "field_data": target["field_data"],
            "timestamp":  datetime.now().isoformat(),
        }
        _save_pending(all_pending)

        merged_fd = _apply_patch(target["field_data"], patch, field_map, protected)

        del_res = await _remove_from_collection(col, target["id"])
        if not del_res.get("ok"):
            return (
                f"❌ Erreur suppression : {del_res.get('error')}\n"
                f"_Snapshot disponible dans reviseur\\_pending.json_"
            )

        add_res = await _add_to_collection(col, slug, merged_fd)
        if not add_res.get("ok"):
            return (
                f"❌ Erreur recréation : {add_res.get('error')}\n"
                f"⚠️ *L'item a été supprimé mais la recréation a échoué.*\n"
                f"Snapshot dans `reviseur\\_pending.json` → clé `{key}[\"last\\_snapshot\"]`"
            )

        # Mark only safe (non-blocked) recs as applied
        applied_nums = {r["num"] for r in safe_recs}
        for r in recs:
            if r.get("num") in applied_nums:
                r["applied"] = True
        _save_pending(all_pending)

        await _publish_staging()

        staging_url = ""
        if FRAMER_STAGING_URL:
            staging_url = FRAMER_STAGING_URL.rstrip("/") + f"/{slug}"

        modified = list(patch.keys())
        lines = [
            f"✅ *Patch appliqué — {target['title']}*\n",
            f"Champs modifiés : `{'`, `'.join(modified)}`",
            f"Recs appliquées : #{', #'.join(str(n) for n in sorted(applied_nums))}",
        ]
        if blocked:
            blocked_nums = ", ".join("#" + str(r["num"]) for r in blocked)
            lines.append(f"⛔ Bloquées (champs protégés) : {blocked_nums}")
        if staging_url:
            lines.append(f"\n👁 {staging_url}")
        return "\n".join(lines)

    # ── Commande : éditer ─────────────────────────────────────────────────────

    async def cmd_editer(self, context: dict | None = None) -> str:
        ctx   = context or {}
        col   = ctx.get("collection")
        slug  = ctx.get("slug")
        champ = ctx.get("champ")
        val   = ctx.get("valeur")

        if not all([col, slug, champ, val]):
            return (
                "❌ Usage :\n"
                "`/reviseur éditer --collection <id> --slug <slug> --champ <champ> --valeur <valeur>`\n\n"
                "_Modification directe d'un seul champ — 0 token Claude._"
            )

        list_res = await _list_collection(col)
        if not list_res.get("ok"):
            return f"❌ Erreur Framer : {list_res.get('error')}"

        items  = list_res.get("items", [])
        target = next((i for i in items if i["slug"] == slug), None)
        if not target:
            return f"❌ Slug `{slug}` introuvable dans `{col}`."

        field_map = _get_field_map(col) or _build_dynamic_field_map(items)
        guide     = _load_guide().get(col, {})
        protected = _get_protected_fields(guide)
        if champ in protected:
            return f"⛔ Le champ `{champ}` est protégé dans le guide de `{col}` — modification refusée."
        merged    = _apply_patch(target["field_data"], {champ: val}, field_map, protected)

        # Snapshot
        all_pending = _load_pending()
        key = f"{col}:{slug}"
        all_pending.setdefault(key, {})
        all_pending[key]["last_snapshot"] = {
            "slug": slug, "item_id": target["id"],
            "field_data": target["field_data"],
            "timestamp": datetime.now().isoformat(),
        }
        _save_pending(all_pending)

        del_res = await _remove_from_collection(col, target["id"])
        if not del_res.get("ok"):
            return f"❌ Erreur suppression : {del_res.get('error')}"

        add_res = await _add_to_collection(col, slug, merged)
        if not add_res.get("ok"):
            return f"❌ Erreur recréation : {add_res.get('error')}\n⚠️ Snapshot dans reviseur\\_pending.json"

        await _publish_staging()
        return f"✅ Champ `{champ}` mis à jour directement (0 token Claude)."


agent = ReviseurAgent()
