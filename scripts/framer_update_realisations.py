#!/usr/bin/env python3
"""
scripts/framer_update_realisations.py — Met à jour les champs CMS Framer
(titre, sous-titre, meta description) pour les 19 pages de réalisations.

Lit les fichiers Obsidian modifiés (title_seo, sous-titre) + les fichiers d'audit
(meta description) pour construire les nouvelles valeurs, puis les envoie via
framer_helper.js à l'API Framer.

Usage : python scripts/framer_update_realisations.py [--dry-run]
"""
import json, os, re, subprocess, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
VAULT_PATH    = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/Users/welldone/Desktop/Obsidian/WelldoneStudio"))
PROJETS_DIR   = VAULT_PATH / "05-Marketing" / "Site-web" / "projets"
ANALYSE_DIR   = VAULT_PATH / "05-Marketing" / "Site-web" / "analyse" / "pages"
HELPER        = Path(__file__).parent.parent / "framer_helper.js"
NODE          = "/usr/local/bin/node"
COLLECTION_ID = os.environ.get("FRAMER_PROJECTS_COLLECTION_ID", "ZPxiZ9t6y")
API_KEY       = os.environ.get("FRAMER_API_KEY", "")
DRY_RUN       = "--dry-run" in sys.argv

# IDs des champs Framer CMS (obtenus via schema)
FIELD_TITRE    = "N78BlVPND"   # Titre projet (H1 + base du title tag)
FIELD_SUBTITLE = "aJJ9h7Xco"  # Sous-titre / tagline
FIELD_META     = "ew5M1GrrF"  # Meta description

# Mapping slug Framer → (fichier réalisation Obsidian, fichier audit)
SLUG_MAP = {
    "nestor-campagne-publicité-agence-immobilière-québec-montréal":
        ("realisations-nestor-campagne-publicit-c3-a9-agence-immobili-c3-a8re-qu-c3-a9bec-montr-c3-a9al-welldone-studio.md", "nestor.md"),
    "dominique-filion-paysagement-rayonnement":
        ("realisations-dominique-filion-paysagement-rayonnement.md", "dominique-filion.md"),
    "laurie-raphael-brand-storytelling-motion-quebec":
        ("realisations-laurie-raphael-brand-storytelling-motion-quebec.md", "laurie-raphael.md"),
    "florian-artiste-visuel-presence-digitale-site-web":
        ("realisations-florian-artiste-visuel-presence-digitale-site-web.md", "florian.md"),
    "le-petit-laurier-photographie-architecturale-montreal":
        ("realisations-le-petit-laurier-photographie-architecturale-montreal.md", "le-petit-laurier.md"),
    "epix-studio-campagne-positionnement-fitness-montreal":
        ("realisations-epix-studio-campagne-positionnement-fitness-montreal.md", "epix-studio.md"),
    "mcbb-salon-boutique-photographie-commerciale-quebec":
        ("realisations-mcbb-salon-boutique-photographie-commerciale-quebec.md", "mcbb.md"),
    "residence-46n74o-alt-280-photographie-architecturale-mont-tremblant":
        ("realisations-residence-46n74o-alt-280-photographie-architecturale-mont-tremblant.md", "46n74o-alt-280.md"),
    "inspection-vci-site-web-inspection-batiment-quebec-montreal":
        ("realisations-inspection-vci-site-web-inspection-batiment-quebec-montreal.md", "inspection-vci.md"),
    "theblondballet-iam-lola-burlesque-montréal-québec":
        ("realisations-theblondballet-iam-lola-burlesque-montr-c3-a9al-qu-c3-a9bec.md", "iamLOLA-andreanne-mercier.md"),
    "centre-dentaire-repentigny-marque-employeur-video-publicité-montréal":
        ("realisations-centre-dentaire-repentigny-marque-employeur-video-publicit-c3-a9-montr-c3-a9al.md", "centre-dentaire-repentigny.md"),
    "eleonore-photographie-architecturale-multihabitation-montreal":
        ("realisations-eleonore-photographie-architecturale-multihabitation-montreal.md", "eleonore.md"),
    "la-cedriere-blanchette-archi-welldone-archi":
        ("realisations-la-cedriere-blanchette-archi-welldone-archi.md", "la-cedriere.md"),
    "lavender-may-portraiture-burlesque-photography-montreal":
        ("realisations-lavender-may-portraiture-burlesque-photography-montreal.md", "lavender-may.md"),
    "mineral-winebar-photographie-hospitality-design-montreal":
        ("realisations-mineral-winebar-photographie-hospitality-design-montreal.md", "mineral-winebar.md"),
    "strategie-marketing-creation-contenu-restaurant-perles-paddock":
        ("realisations-strategie-marketing-creation-contenu-restaurant-perles-paddock.md", "perles-paddock.md"),
    "le-lionel-restaurant-quartier-solar-photographie-commerciale":
        ("realisations-le-lionel-restaurant-quartier-solar-photographie-commerciale.md", "le-lionel.md"),
    "sweeter-videoclip-welldone-studio":
        ("realisations-sweeter-videoclip-welldone-studio.md", "sweeter-eihdz.md"),
    "wildman-wilderness-lodge-documenter-la-derniere-frontière.":
        ("realisations-wildman-wilderness-lodge-documenter-la-derniere-fronti-c3-a8re.md", "wildman.md"),
}


def _parse_obsidian_file(path: Path) -> dict:
    """Extrait title_seo et sous-titre depuis un fichier Obsidian modifié."""
    content = path.read_text(encoding="utf-8")
    result = {}

    # title_seo depuis le YAML
    m = re.search(r'^title_seo:\s*(.+)$', content, re.MULTILINE)
    if m:
        title_seo = m.group(1).strip()
        # Retirer le suffixe "· Welldone Studio" ou "| Welldone Studio"
        title_seo = re.sub(r'\s*[·|]\s*Welldone Studio$', '', title_seo).strip()
        result["titre"] = title_seo

    # Sous-titre : ligne *italique* juste après le H1
    m = re.search(r'^# .+\n+\*(.+)\*', content, re.MULTILINE)
    if m:
        result["subtitle"] = m.group(1).strip()

    return result


def _parse_audit_meta(path: Path) -> str:
    """Extrait la meta description recommandée (version courte) depuis le fichier d'audit."""
    content = path.read_text(encoding="utf-8")

    # Chercher la version courte (155 caractères)
    m = re.search(r'Version à 155 caractères[^`]*`([^`]+)`', content, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Fallback : première meta recommandée
    m = re.search(r'## Meta description recommandée\s*`([^`]+)`', content)
    if m:
        return m.group(1).strip()

    return ""


def _framer_update(item_id: str, fields: dict) -> dict:
    """Appelle framer_helper.js update avec les champs donnés."""
    field_data = {
        fid: {"type": "string", "value": val}
        for fid, val in fields.items()
    }
    cmd = [
        NODE, str(HELPER), "update", item_id, json.dumps(field_data)
    ]
    env = {**os.environ, "FRAMER_API_KEY": API_KEY, "FRAMER_COLLECTION_ID": COLLECTION_ID}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"ok": False, "error": result.stdout + result.stderr}


def _get_framer_items() -> dict:
    """Retourne {slug: item_id} depuis l'API Framer."""
    cmd = [NODE, str(HELPER), "list"]
    env = {**os.environ, "FRAMER_API_KEY": API_KEY, "FRAMER_COLLECTION_ID": COLLECTION_ID}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    data = json.loads(result.stdout)
    return {item["slug"]: item["id"] for item in data.get("items", [])}


def main():
    if not API_KEY:
        print("❌ FRAMER_API_KEY manquant")
        sys.exit(1)

    print(f"{'🔎 DRY-RUN — aucune modification' if DRY_RUN else '🚀 Mise à jour Framer CMS'}\n")

    # Récupérer les IDs Framer actuels
    print("📡 Connexion Framer...")
    slug_to_id = _get_framer_items()
    print(f"   {len(slug_to_id)} items trouvés\n")

    success = errors = skipped = 0

    for framer_slug, (obs_file, audit_file) in SLUG_MAP.items():
        item_id = slug_to_id.get(framer_slug)
        if not item_id:
            # Essai avec slug partiel (troncature)
            for slug, iid in slug_to_id.items():
                if slug.startswith(framer_slug[:40]):
                    item_id = iid
                    break

        if not item_id:
            print(f"  ⚠️  Slug introuvable dans Framer : {framer_slug[:50]}")
            skipped += 1
            continue

        # Lire les fichiers Obsidian
        obs_path   = PROJETS_DIR / obs_file
        audit_path = ANALYSE_DIR / audit_file

        if not obs_path.exists():
            print(f"  ⚠️  Obsidian manquant : {obs_file}")
            skipped += 1
            continue

        obs_data = _parse_obsidian_file(obs_path)
        meta     = _parse_audit_meta(audit_path) if audit_path.exists() else ""

        fields = {}
        if obs_data.get("titre"):
            fields[FIELD_TITRE] = obs_data["titre"]
        if obs_data.get("subtitle"):
            fields[FIELD_SUBTITLE] = obs_data["subtitle"]
        if meta:
            fields[FIELD_META] = meta

        if not fields:
            print(f"  ·  {obs_file[:50]} — rien à mettre à jour")
            skipped += 1
            continue

        titre_display = obs_data.get("titre", "?")[:50]
        print(f"  {'[DRY]' if DRY_RUN else '→'} {titre_display}")
        if obs_data.get("subtitle"):
            print(f"       subtitle: {obs_data['subtitle'][:60]}")
        if meta:
            print(f"       meta:     {meta[:60]}...")

        if not DRY_RUN:
            result = _framer_update(item_id, fields)
            if result.get("ok"):
                print(f"       ✅ mis à jour ({', '.join(result.get('fields', []))})")
                success += 1
            else:
                print(f"       ❌ erreur : {result.get('error', '?')}")
                errors += 1
        else:
            success += 1

        print()

    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✅ {success} mis à jour  |  ⚠️ {skipped} ignorés  |  ❌ {errors} erreurs")
    if DRY_RUN:
        print("\nRelance sans --dry-run pour appliquer.")


if __name__ == "__main__":
    main()
