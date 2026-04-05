#!/usr/bin/env python3
"""
scripts/format_crawl_projets.py — Reformateur de tous les fichiers crawlés dans Site-web/.

Transforme chaque fichier .md sous 05-Marketing/Site-web/ (projets, articles, pages, services) :
1. URL cliquable dans le body (sort du YAML rouge)
2. Métadonnées (Localisation, Secteur, Type de mandat, Objectif...) → table gauche-droite
3. Supprime les lignes parasites (Défilez vers le bas, titre en double, blocs carousel, etc.)

Usage : python scripts/format_crawl_projets.py
"""
import re, sys
from pathlib import Path

VAULT_PATH   = Path("/Users/welldone/Desktop/Obsidian/WelldoneStudio")
SITE_WEB_DIR = VAULT_PATH / "05-Marketing" / "Site-web"

# Sous-dossiers à traiter (on exclut analyse/ qui contient nos propres docs)
TARGET_DIRS = ["projets", "articles", "pages", "services"]

# Labels de métadonnées reconnus (ordre de priorité dans la table)
META_LABELS = [
    "Localisation",
    "Année",
    "Industrie",
    "Secteur d'activité",
    "Secteur",
    "Type de mandat",
    "Livrables",
    "Objectif Stratégique",
    "Objectif",
]

# Lignes parasites à supprimer
NOISE_LINES = {
    "Défilez vers le bas pour voir plus de contenu",
    "Défilezverslebaspourvoirplusdecontenu",
    "En savoir Plus",
    "En savoir plus",
    "Credits",
    "Réalisation",
    "Production",
    "Retour",
    "View Project",
    "Voir le projet",
    "Montage",
    "Artiste de voix",
    "Direction artistique",
    "Direction de création",
    "Photographie",
    "Vidéographie",
    "Welldone Studio",
    "PROPULSONS VOTRE IMAGE",
    "ON S'EN OCCUPE",
    "Voir Notre Portfolio",
    "View",
}

NOISE_PATTERNS = [
    r"^↓$",
    r"^\[↓\]",
    r"^#+\s*Ils nous ont fait confiance",
    r"^#+\s*PROPULSONS VOTRE IMAGE",
    r"^\[Welldone Studio\]",
    # Liens de carousel projet (View ↓ embedded in link text)
    r"^\[.*\\\\.*View.*\\\\.*↓\]",
    # Lien "Voir Notre Portfolio"
    r"^\[Voir Notre Portfolio\]",
    # Lien "Lien Projet" générique (articles)
    r"^\[Lien Projet\]\(",
    # Lien avec texte Na/N/A
    r"^\[Na\]\(",
    r"^\[N/A\]\(",
    # Liens mailto
    r"^\[.*\]\(mailto:",
    # Lignes de séparation "ON S'EN OCCUPE"
    r"^\[ON S'EN OCCUPE\]",
    # FAQ label seul
    r"^FAQ$",
]


def _extract_frontmatter(content: str) -> tuple[dict, str]:
    """Extrait le YAML frontmatter et retourne (meta_dict, body)."""
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    yaml_block = content[4:end].strip()
    body = content[end + 4:].lstrip("\n")

    meta = {}
    for line in yaml_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()

    return meta, body


def _find_meta_pairs(lines: list[str]) -> dict[str, str]:
    """
    Détecte les paires label → valeur dans le body.
    Pattern : ligne label connue, (ligne vide), ligne valeur.
    """
    pairs = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in META_LABELS:
            # Chercher la valeur dans les 3 prochaines lignes
            for j in range(i + 1, min(i + 4, len(lines))):
                val = lines[j].strip()
                if val and val not in META_LABELS and val not in NOISE_LINES:
                    pairs[stripped] = val
                    break
        i += 1
    return pairs


def _build_meta_table(pairs: dict[str, str]) -> str:
    """Construit la table Markdown gauche-droite."""
    if not pairs:
        return ""

    # Ordre canonique
    ordered = []
    for label in META_LABELS:
        if label in pairs:
            ordered.append((label, pairs[label]))
    # Ajouter les paires non reconnues
    for k, v in pairs.items():
        if k not in META_LABELS:
            ordered.append((k, v))

    lines = ["| | |", "|:--|--:|"]
    for label, value in ordered:
        # Nettoyer les valeurs parasites ("Na", "N/A", etc.)
        if value.lower() in ("na", "n/a", "–", "-", ""):
            continue
        lines.append(f"| **{label}** | {value} |")

    return "\n".join(lines) if len(lines) > 2 else ""


def _clean_body(body: str, title: str, url: str, meta_pairs: dict) -> str:
    """Nettoie le body et restructure."""
    # ── Suppression globale avant split : blocs carousel multi-lignes ────────
    # Pattern : [Titre\\\n...\\\nView\\\n...\\\n↓](url)
    body = re.sub(
        r'\[[^\]]*\\+\n(?:[^\]\n]*\n)*[^\]\n]*↓\]\([^)]*\)',
        '',
        body,
    )
    # Supprimer les sections entières après certains titres
    body = re.sub(
        r'#{1,3}\s*Ils nous ont fait confiance.*',
        '',
        body,
        flags=re.DOTALL,
    )
    body = re.sub(
        r'#{1,3}\s*PROPULSONS VOTRE IMAGE.*',
        '',
        body,
        flags=re.DOTALL,
    )
    # Supprimer les Métadonnées crawl en bas de fichier
    body = re.sub(
        r'\n---\s*\n\*\*Métadonnées crawl\*\*.*',
        '',
        body,
        flags=re.DOTALL,
    )

    lines = body.splitlines()
    cleaned = []
    skip_next = False
    meta_block_done = False

    # Lignes qui correspondent à des métadonnées (labels + valeurs) → à supprimer du body
    meta_labels_set = set(META_LABELS)
    meta_values_set = set(meta_pairs.values()) if meta_pairs else set()

    for i, line in enumerate(lines):
        stripped = line.strip()

        if skip_next:
            skip_next = False
            if not stripped:
                continue

        # Supprimer les lignes parasites
        if stripped in NOISE_LINES:
            continue
        if any(re.search(p, stripped) for p in NOISE_PATTERNS):
            continue

        # Supprimer le titre en double (texte nu, sans #)
        if stripped and stripped == title and not stripped.startswith("#"):
            continue

        # Supprimer les labels de métadonnées et leurs valeurs
        if stripped in meta_labels_set:
            continue
        if stripped in meta_values_set and not stripped.startswith("#"):
            continue

        # Supprimer le tagline répétitif (première ligne type "Studio | Secteur | ...")
        if i < 5 and "|" in stripped and not stripped.startswith("|") and not stripped.startswith("#"):
            continue

        cleaned.append(line)

    # Retirer les blocs de lignes vides excessives
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned))
    return result.strip()


def reformat_file(path: Path) -> bool:
    """Reformate un fichier. Retourne True si modifié."""
    content = path.read_text(encoding="utf-8")
    meta, body = _extract_frontmatter(content)

    url      = meta.get("url", "")
    title    = meta.get("title", "")
    site     = meta.get("site", "studio")

    # Détecter les paires de métadonnées dans le body original
    meta_pairs = _find_meta_pairs(body.splitlines())

    # Construire la table
    meta_table = _build_meta_table(meta_pairs)

    # Nettoyer le body
    body_clean = _clean_body(body, title, url, meta_pairs)

    # ── Construire le YAML minimal (sans url — elle va dans le body) ──────────
    yaml_lines = ["---"]
    for key in ("type", "site", "slug", "title", "crawl_date"):
        if meta.get(key):
            yaml_lines.append(f"{key}: {meta[key]}")
    yaml_lines.append("---")
    yaml_block = "\n".join(yaml_lines)

    # ── Construire le lien URL cliquable ──────────────────────────────────────
    if url:
        domain = "welldone.archi" if site == "archi" else "awelldone.com"
        url_link = f"[→ Voir sur {domain}]({url})"
    else:
        url_link = ""

    # ── Assembler le fichier final ────────────────────────────────────────────
    parts = [yaml_block, ""]

    # H1 titre (toujours depuis le frontmatter — source de vérité)
    if title:
        parts.append(f"# {title}")
        parts.append("")

    # Lien cliquable
    if url_link:
        parts.append(url_link)
        parts.append("")

    # Table métadonnées
    if meta_table:
        parts.append(meta_table)
        parts.append("")
        parts.append("---")
        parts.append("")

    # Body nettoyé — toujours retirer le H1 du body (déjà dans parts)
    body_final = re.sub(r"^# .+\n\n?", "", body_clean, count=1) if title else body_clean
    parts.append(body_final.strip())

    new_content = "\n".join(parts).rstrip() + "\n"

    if new_content == content:
        return False

    path.write_text(new_content, encoding="utf-8")
    return True


def main():
    # Collecter tous les fichiers .md des dossiers cibles (pas les _index et _rapport)
    all_files = []
    for subdir in TARGET_DIRS:
        d = SITE_WEB_DIR / subdir
        if d.exists():
            files = sorted(f for f in d.glob("*.md") if not f.name.startswith("_"))
            all_files.extend(files)

    if not all_files:
        print(f"❌ Aucun fichier trouvé dans {SITE_WEB_DIR}")
        sys.exit(1)

    print(f"📂 {len(all_files)} fichiers à reformater\n")
    modified = 0
    current_dir = None

    for path in all_files:
        if path.parent != current_dir:
            current_dir = path.parent
            print(f"\n  📁 {current_dir.name}/")

        try:
            changed = reformat_file(path)
            status = "✅" if changed else "·"
            print(f"    {status} {path.name}")
            if changed:
                modified += 1
        except Exception as e:
            print(f"    ❌ {path.name} — {e}")

    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✅ {modified} fichiers reformatés sur {len(all_files)}")


if __name__ == "__main__":
    main()
