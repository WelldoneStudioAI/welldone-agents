#!/usr/bin/env python3
"""
scripts/fix_crawl_spacing.py — Correcteur de texte post-crawl Firecrawl + Framer.

Framer anime les titres en découpant les mots en <span> individuels.
Firecrawl les concatène sans espaces → "Transformerl'imageenactif..."

Usage :
  python scripts/fix_crawl_spacing.py
  python scripts/fix_crawl_spacing.py --dry-run
"""
import os, re, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

VAULT_PATH    = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/Users/welldone/Desktop/Obsidian/WelldoneStudio"))
SITE_WEB_DIR  = VAULT_PATH / "05-Marketing" / "Site-web"
DRY_RUN       = "--dry-run" in sys.argv
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Détection conservatrice des lignes problématiques ─────────────────────────

def _has_merged_words(text: str) -> bool:
    """Patterns très sûrs de mots collés — split-text Framer."""
    # Retirer liens markdown et balises # pour analyser le texte brut
    clean = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    clean = re.sub(r'^#+\s*', '', clean).strip()

    if not clean:
        return False

    # Pattern 1 : minuscule → majuscule → minuscule (camelCase parasite)
    # "studioServices", "Transformerl'image", "desstratégies"
    if re.search(r'[a-zàâéèêëîïôùûü][A-ZÀÂÉÈÊËÎÏÔÙÛÜ][a-zàâéèêëîïôùûü]', clean):
        return True

    # Pattern 2 : article/préposition français collé sans espace
    # "Desstratégies" "Lesstratégies" "Dansle" "Pourles"
    if re.search(r'\b(Des|Les|Dans|Pour|Avec|Vers|Aux|Sous|Une|Vos|Nos|Par)[a-zàâéèêëîïôùûü]', clean):
        return True

    # Pattern 3 : mot unique > 30 chars sans tiret/point/underscore
    for word in clean.split():
        w = re.sub(r'[®™©°\*\|#]', '', word)
        if (len(w) > 30
                and not any(c in w for c in ['-', '_', '.', '/'])
                and not w.startswith('http')):
            return True

    return False


def _needs_correction(line: str, in_frontmatter: bool) -> bool:
    stripped = line.strip()
    if not stripped or stripped == '---':
        return False
    if in_frontmatter:
        return False  # ne jamais corriger les métadonnées YAML

    # Skip : lignes techniques / liens / tableaux / métadonnées crawl
    if stripped.startswith(('http', '|', '- **', '**Métadonnées', '![')):
        return False
    if re.match(r'^[-*]\s+\*\*(URL|H1|H2|Meta|CTAs|Liens)', stripped):
        return False

    # Skip si la ligne est surtout un lien markdown
    link_stripped = re.sub(r'\[([^\]]*)\]\([^)]*\)', '', stripped).strip()
    if len(link_stripped) < 5:
        return False

    return _has_merged_words(stripped)


# ── Correction via Claude ──────────────────────────────────────────────────────

def fix_with_claude(lines: list[str]) -> dict[str, str]:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    batch = "\n".join(f"[{i}] {line}" for i, line in enumerate(lines))

    prompt = f"""Tu corriges du texte français dont les mots ont été collés par une erreur de parsing web
(animations CSS Framer split-text qui découpent les mots en <span>).

RÈGLE ABSOLUE : Réinsère uniquement les espaces manquants entre les mots.
Ne change PAS : le sens, les majuscules, la ponctuation, les # Markdown, les symboles ®™.
Si une ligne est déjà correcte, retourne-la inchangée.

EXEMPLES :
  "Transformerl'imageenactifstratégique" → "Transformer l'image en actif stratégique"
  "Desstratégiesvisuellespensées" → "Des stratégies visuelles pensées"
  "## Photographied'architecture" → "## Photographie d'architecture"
  "Welldone|Studio®" → "Welldone|Studio®" (inchangé — marque)
  "PME de Québec" → "PME de Québec" (inchangé — déjà correct)

Réponds avec EXACTEMENT ce format, une ligne par entrée :
[0] texte corrigé
[1] texte corrigé
...

Lignes :
{batch}"""

    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    result = {}
    for line in resp.content[0].text.strip().splitlines():
        m = re.match(r'\[(\d+)\]\s+(.*)', line)
        if m:
            idx = int(m.group(1))
            if idx < len(lines):
                fixed = m.group(2).strip()
                if fixed != lines[idx]:  # garder seulement les vrais changements
                    result[lines[idx]] = fixed
    return result


# ── Application des corrections ───────────────────────────────────────────────

def apply_to_file(path: Path, corrections: dict[str, str]) -> int:
    original = path.read_text(encoding="utf-8")
    lines    = original.splitlines()
    new_lines, count = [], 0

    for line in lines:
        stripped = line.strip()
        if stripped in corrections:
            indent = len(line) - len(line.lstrip())
            new_lines.append(" " * indent + corrections[stripped])
            count += 1
        else:
            new_lines.append(line)

    if count:
        path.write_text("\n".join(new_lines), encoding="utf-8")
    return count


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not SITE_WEB_DIR.exists():
        print(f"❌ Dossier introuvable : {SITE_WEB_DIR}")
        sys.exit(1)
    if not ANTHROPIC_KEY and not DRY_RUN:
        print("❌ ANTHROPIC_API_KEY manquante")
        sys.exit(1)

    md_files = [p for p in SITE_WEB_DIR.rglob("*.md") if not p.name.startswith("_")]
    print(f"📂 {len(md_files)} fichiers à analyser\n")

    # Collecter les lignes problématiques (dédupliquées)
    all_problems: dict[str, list[Path]] = {}

    for path in md_files:
        content = path.read_text(encoding="utf-8")
        in_fm   = False
        fm_count = 0

        for line in content.splitlines():
            if line.strip() == '---':
                fm_count += 1
                in_fm = (fm_count == 1)
                if fm_count == 2:
                    in_fm = False
                continue

            stripped = line.strip()
            if _needs_correction(stripped, in_fm):
                all_problems.setdefault(stripped, []).append(path)

    if not all_problems:
        print("✅ Aucune ligne problématique détectée.")
        return

    print(f"🔍 {len(all_problems)} lignes problématiques :")
    for line in list(all_problems.keys())[:15]:
        print(f"  → {line[:90]}")
    if len(all_problems) > 15:
        print(f"  ... et {len(all_problems) - 15} autres")

    if DRY_RUN:
        print("\n🔎 Dry-run — aucun fichier modifié.")
        return

    print()

    # Batches de 20 → Claude
    lines_list = list(all_problems.keys())
    BATCH = 20
    corrections: dict[str, str] = {}

    for i in range(0, len(lines_list), BATCH):
        batch = lines_list[i:i + BATCH]
        n = i // BATCH + 1
        total = (len(lines_list) - 1) // BATCH + 1
        print(f"🤖 Batch {n}/{total} ({len(batch)} lignes)...")
        fixed = fix_with_claude(batch)
        corrections.update(fixed)
        changed = len(fixed)
        print(f"   ✓ {changed} correction(s) effectuée(s)")

    print()

    # Appliquer
    total_files = total_fixes = 0
    for path in md_files:
        count = apply_to_file(path, corrections)
        if count:
            total_files += 1
            total_fixes += count
            print(f"  ✅ {path.parent.name}/{path.name} — {count} fix(es)")

    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✅ {total_fixes} corrections dans {total_files} fichiers")


if __name__ == "__main__":
    main()
