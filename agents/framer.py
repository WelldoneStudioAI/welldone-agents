"""
agents/framer.py — Agent Framer CMS pour le blog de Welldone Studio.

Architecture:
  Telegram → agents/framer.py (Python) → framer_helper.js (Node.js SDK WebSocket) → Framer CMS

Collection cible : ERDJzzQHr (Welldone Studio-Blog)
Field map extrait live depuis l'article Wildman (référence).

Images libres de droits:
  - Unsplash API si UNSPLASH_ACCESS_KEY dispo
  - LoremFlickr sinon (aucune clé, CC)
"""
import json, logging, os, re, subprocess, urllib.request, urllib.error, urllib.parse
from pathlib import Path

from agents._base import BaseAgent
from core.brain import get_client
from config import CLAUDE_MODEL, FRAMER_API_KEY, FRAMER_COLLECTION_ID, UNSPLASH_ACCESS_KEY

log = logging.getLogger(__name__)

_HELPER = Path(__file__).parent.parent / "framer_helper.js"

# ── Field map confirmé via Wildman Wilderness (référence live) ────────────────
# IDs découverts en inspectant les champs de ERDJzzQHr via le SDK
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

# ── Prompt de génération ──────────────────────────────────────────────────────
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


# ── Subprocess helper ─────────────────────────────────────────────────────────
def _run_helper(command: str, arg: str = "") -> dict:
    env = {**os.environ,
           "FRAMER_API_KEY": FRAMER_API_KEY,
           "FRAMER_COLLECTION_ID": FRAMER_COLLECTION_ID}
    cmd = ["node", str(_HELPER), command]
    if arg:
        cmd.append(arg)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        out = r.stdout.strip()
        if not out:
            return {"ok": False, "error": r.stderr.strip() or "Pas de réponse du helper"}
        return json.loads(out)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout 60s — connexion Framer trop longue"}
    except FileNotFoundError:
        return {"ok": False, "error": "Node.js introuvable — vérifier le Dockerfile"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Images libres de droits ───────────────────────────────────────────────────
def _search_unsplash(queries: list[str]) -> list[dict]:
    results = []
    for query in queries[:3]:
        try:
            q   = urllib.parse.quote(query)
            url = f"https://api.unsplash.com/search/photos?query={q}&per_page=3&orientation=landscape"
            req = urllib.request.Request(url, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"})
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
    """LoremFlickr — photos CC libres de droits, aucune clé."""
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


# ── Slug ──────────────────────────────────────────────────────────────────────
def _make_slug(text: str) -> str:
    s = text.lower().strip()
    for src, dst in [("é","e"),("è","e"),("ê","e"),("à","a"),("â","a"),("ô","o"),
                     ("î","i"),("û","u"),("ù","u"),("ç","c"),("ë","e"),("ï","i")]:
        s = s.replace(src, dst)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


# ── Agent ─────────────────────────────────────────────────────────────────────
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
        Génère un article complet (47 champs) et le publie en brouillon dans Framer.
        context: { sujet: str }
        """
        ctx   = context or {}
        sujet = ctx.get("sujet", ctx.get("message", "")).strip()

        if not sujet:
            return "❌ Paramètre `sujet` manquant. Ex: « Rédige un article sur comment choisir son photographe corporatif à Montréal »"
        if not FRAMER_API_KEY:
            return "❌ `FRAMER_API_KEY` manquant dans les variables Railway."

        # ── 1. Générer le contenu structuré avec Claude ───────────────────────
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

        # ── 2. Images libres de droits ────────────────────────────────────────
        img_queries = article.get("image_queries",
                                  [sujet, "professional photography Quebec", "business Montreal"])
        images = _get_images(img_queries)
        img_source = "Unsplash" if UNSPLASH_ACCESS_KEY else "LoremFlickr"

        # Assigner les images aux champs + override alt depuis Claude
        for i, field in enumerate(IMAGE_FIELDS):
            if i < len(images):
                alt_key = f"{field}:alt"
                article[field] = {
                    "src": images[i]["src"],
                    "alt": article.get(alt_key) or images[i].get("alt", ""),
                }

        # ── 3. Construire le fieldData Framer (IDs exacts) ───────────────────
        slug = _make_slug(article.get("slug") or article.get("Title") or sujet)
        field_data: dict = {}

        for col_name, meta in FIELD_MAP.items():
            fid   = meta["id"]
            ftype = meta["type"]

            if ftype == "image":
                # Framer attend { src, alt } pour les images
                val = article.get(col_name)
                if isinstance(val, dict) and val.get("src"):
                    field_data[fid] = {"value": val, "type": "image"}
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

        framer_item = {"slug": slug, "fieldData": field_data}

        # ── 4. Push vers Framer CMS via Node.js helper ────────────────────────
        log.info(f"framer.rediger: push slug={slug} fields={len(field_data)}")
        result = _run_helper("create", json.dumps(framer_item))

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
                f"❌ Erreur: {err[:300]}\n\n"
                f"💡 Relance `railway up` si c'est un problème de connexion."
            )

    async def liste(self, context: dict | None = None) -> str:
        """Liste les articles existants dans Framer CMS."""
        if not FRAMER_API_KEY:
            return "❌ `FRAMER_API_KEY` manquant dans Railway."

        result = _run_helper("list")
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

        result = _run_helper("delete", item_id)
        if result.get("ok"):
            return f"🗑️ Article `{item_id}` supprimé de Framer CMS."
        return f"❌ Erreur: {result.get('error', 'Inconnu')}"


agent = FramerAgent()
