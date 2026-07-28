"""
core/foncier/sources/centris.py — Alertes Centris reçues par courriel.

Le côté « déjà à vendre » de la thèse, obtenu sans jamais visiter Centris.

COMMENT ÇA MARCHE
  1. Tu configures une alerte de recherche sauvegardée sur centris.ca avec tes
     critères (commercial et multirésidentiel, 1 à 5 M$, tes secteurs).
  2. Centris t'envoie par courriel les nouvelles fiches correspondantes.
  3. Ce module lit ces courriels dans ta boîte et en extrait les liens et les
     faits (adresse, prix demandé, nombre de logements).
  4. L'agent croise avec ta base : « cette fiche, c'est le dossier que tu
     surveilles depuis huit mois à 72 points ».

POURQUOI CE CHEMIN PLUTÔT QUE L'EXTRACTION DIRECTE
  · Centris pousse la donnée volontairement — c'est le service prévu pour ça
  · le format d'un courriel est stable, le HTML d'un site change sans préavis
  · aucun blocage, aucune limite de débit, aucun compte à risque
  · le filtrage est fait par Centris, gratuitement, avant l'envoi

CE QUI EST CONSERVÉ
  Le lien, et les faits bruts : adresse, prix demandé, nombre de logements,
  numéro Centris. Des faits, pas des œuvres — ni photo, ni texte descriptif.
  La fiche elle-même se consulte sur centris.ca, en cliquant le lien.

⚠️ CALIBRATION REQUISE
   Le format exact des courriels d'alerte Centris n'a pas pu être observé à
   l'écriture de ce module. Le parseur emploie plusieurs stratégies et rapporte
   toujours ce qu'il a trouvé ET ce qu'il a manqué. Lancer d'abord :

       python dispatch.py foncier calibrer --fichier alerte.eml

   puis ajuster les motifs ci-dessous selon le rapport.
"""
from __future__ import annotations

import base64
import html as html_module
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from core.foncier import adresse as adr
from core.foncier.modele import Immeuble, Signal

log = logging.getLogger(__name__)

# Expéditeurs considérés comme des alertes Centris.
EXPEDITEURS = tuple(
    e.strip()
    for e in os.environ.get("CENTRIS_EXPEDITEURS", "centris.ca").split(",")
    if e.strip()
)

# ─────────────────────────────────────────────────────────────────────────────
# Motifs d'extraction
# ─────────────────────────────────────────────────────────────────────────────

# Lien vers une fiche. Le numéro Centris (7 à 9 chiffres) termine le chemin.
MOTIF_URL = re.compile(r"https?://(?:www\.)?centris\.ca/[^\s\"'<>)\]]+", re.IGNORECASE)
MOTIF_NO_CENTRIS = re.compile(r"(?<!\d)(\d{7,9})(?!\d)")

# Prix : « 2 400 000 $ », « 2,400,000$ », « 2 400 000 $ ».
MOTIF_PRIX = re.compile(r"(\d{1,3}(?:[\s ,]\d{3}){1,3})\s*\$")

# Adresse civique suivie d'un type de voie.
MOTIF_ADRESSE = re.compile(
    r"\b(\d{1,6}(?:\s*[-–]\s*\d{1,6})?)[,\s]+"
    r"((?:rue|avenue|av\.?|boulevard|boul\.?|chemin|ch\.?|route|rang|place|"
    r"montée|montee|côte|cote|terrasse|impasse|croissant|allée|allee|carré|carre|"
    r"promenade)\s+[^\n,;|]{2,60})",
    re.IGNORECASE,
)

# Nombre de logements.
MOTIF_LOGEMENTS = re.compile(
    r"(\d{1,3})\s*(?:logements?|unités?|unites?|portes?)\b", re.IGNORECASE
)

# Type d'immeuble tel que Centris le nomme.
TYPES_CENTRIS = (
    "quintuplex", "quadruplex", "quintuplex", "triplex", "duplex",
    "immeuble à revenus", "immeuble a revenus", "immeuble résidentiel",
    "immeuble residentiel", "immeuble commercial", "local commercial",
    "bâtisse commerciale", "batisse commerciale", "terrain",
    "édifice de bureaux", "edifice de bureaux", "commerce",
    "multi-logements", "multilogements",
)

# Municipalité : souvent après le type, ou en fin de ligne d'adresse.
MOTIF_MUNICIPALITE = re.compile(
    r"\b(?:à|a)\s+vendre[^\n|]*?[~\-—]\s*([A-ZÀ-Ü][\w\s\-'’]{2,40})", re.IGNORECASE
)

# Fenêtre de texte, en caractères, fouillée autour de chaque lien.
FENETRE = 700


@dataclass
class FicheCentris:
    """Une fiche repérée dans un courriel d'alerte. Faits seulement."""

    no_centris: str
    url: str
    adresse: str = ""
    municipalite: str = ""
    prix_demande: Optional[float] = None
    type_declare: str = ""
    nombre_logements: Optional[int] = None
    vu_le: str = ""
    sujet_courriel: str = ""

    @property
    def complete(self) -> bool:
        """Une fiche est exploitable si on peut la rapprocher d'un immeuble."""
        return bool(self.adresse and self.prix_demande)

    def resume(self) -> str:
        prix = (
            f"{self.prix_demande:,.0f} $".replace(",", " ")
            if self.prix_demande else "prix non extrait"
        )
        lieu = f"{self.adresse}, {self.municipalite}" if self.municipalite else self.adresse
        return f"#{self.no_centris} — {lieu or 'adresse non extraite'} — {prix}"


@dataclass
class Diagnostic:
    """
    Ce que l'extraction a réussi et raté.

    Sans ça, un parseur mal calibré rend « 0 fiche » et on croit qu'il n'y a
    rien sur le marché alors que c'est le format du courriel qui a changé.
    """

    courriels_lus: int = 0
    liens_trouves: int = 0
    fiches_uniques: int = 0
    avec_adresse: int = 0
    avec_prix: int = 0
    avec_municipalite: int = 0
    avec_logements: int = 0
    echantillon_texte: str = ""
    avertissements: list[str] = field(default_factory=list)

    def rapport(self) -> str:
        lignes = [
            f"Courriels lus          : {self.courriels_lus}",
            f"Liens Centris trouvés  : {self.liens_trouves}",
            f"Fiches uniques         : {self.fiches_uniques}",
        ]
        if self.fiches_uniques:
            lignes += [
                f"  · avec adresse       : {self.avec_adresse}/{self.fiches_uniques}",
                f"  · avec prix          : {self.avec_prix}/{self.fiches_uniques}",
                f"  · avec municipalité  : {self.avec_municipalite}/{self.fiches_uniques}",
                f"  · avec nb logements  : {self.avec_logements}/{self.fiches_uniques}",
            ]
        for avertissement in self.avertissements:
            lignes.append(f"⚠️  {avertissement}")
        return "\n".join(lignes)


# ─────────────────────────────────────────────────────────────────────────────
# Nettoyage
# ─────────────────────────────────────────────────────────────────────────────


def html_vers_texte(contenu: str) -> str:
    """
    Réduit un courriel HTML en texte lisible, en préservant les URL EN PLACE.

    Le détail qui compte : l'URL d'une fiche vit dans l'attribut `href`, donc à
    l'intérieur d'une balise. Un nettoyage naïf qui supprime les balises efface
    les liens — et sans repère de position, chaque fiche hérite des chiffres de
    sa voisine. On extrait donc le href comme texte, à la position de l'ancre,
    AVANT de retirer les balises.
    """
    texte = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", contenu)

    # L'ancre devient son URL, au bon endroit dans le flux du texte.
    texte = re.sub(
        r"""(?is)<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>""", r" \1 ", texte
    )

    # Les balises de bloc deviennent des sauts de ligne pour garder la structure.
    texte = re.sub(r"(?i)<(br|/tr|/td|/p|/div|/li|/h[1-6])[^>]*>", "\n", texte)
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = html_module.unescape(texte)
    texte = texte.replace(" ", " ")
    texte = re.sub(r"[ \t]+", " ", texte)
    return re.sub(r"\n\s*\n+", "\n", texte)


def _deballer_redirections(contenu: str) -> str:
    """
    Décode les URL encodées dans les liens de suivi.

    Les infolettres routent souvent par un domaine de tracking qui porte l'URL
    réelle en paramètre encodé. Sans ce décodage, aucun lien centris.ca n'est
    visible dans le courriel.
    """
    try:
        decode = urllib.parse.unquote(contenu)
    except Exception:
        return contenu
    # On conserve les deux versions : le lien peut être direct OU encodé.
    return contenu + "\n" + decode if decode != contenu else contenu


def _nombre(texte: str) -> Optional[float]:
    nettoye = re.sub(r"[^\d]", "", texte or "")
    return float(nettoye) if nettoye else None


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────


def _no_centris_depuis_url(url: str) -> Optional[str]:
    """Extrait le numéro Centris d'une URL de fiche."""
    chemin = url.split("?")[0].split("#")[0].rstrip("/")
    numeros = MOTIF_NO_CENTRIS.findall(chemin)
    # Le numéro de fiche est le dernier segment numérique du chemin.
    return numeros[-1] if numeros else None


def extraire(
    contenu: str, sujet: str = "", diagnostic: Optional[Diagnostic] = None
) -> list[FicheCentris]:
    """
    Extrait les fiches d'un courriel d'alerte.

    Args:
        contenu: corps du courriel, HTML ou texte
        sujet: sujet du courriel, conservé pour la traçabilité
        diagnostic: compteur alimenté au passage (optionnel)

    Returns:
        Les fiches trouvées, dédupliquées par numéro Centris.
    """
    diagnostic = diagnostic if diagnostic is not None else Diagnostic()
    contenu = _deballer_redirections(contenu or "")
    texte = html_vers_texte(contenu)

    if not diagnostic.echantillon_texte:
        diagnostic.echantillon_texte = texte[:1500]

    urls = MOTIF_URL.findall(contenu) + MOTIF_URL.findall(texte)
    diagnostic.liens_trouves += len(urls)

    fiches: dict[str, FicheCentris] = {}
    aujourdhui = date.today().isoformat()

    # Chaque fiche est bornée par la suivante : sans ça, une alerte compacte
    # fait hériter à la fiche 1 le prix et le nombre de logements de la fiche 2.
    for no_centris, url, strict, elargi in _decouper_en_blocs(texte, urls):
        if no_centris in fiches:
            continue

        fiche = FicheCentris(
            no_centris=no_centris,
            url=url,
            vu_le=aujourdhui,
            sujet_courriel=sujet[:150],
        )
        _renseigner(fiche, strict)

        # Repli : certaines mises en page placent l'adresse au-dessus du lien.
        # On n'y recourt que si le bloc strict n'a rien donné, pour ne pas
        # aspirer les chiffres de la fiche précédente dans le cas normal.
        if not fiche.adresse and not fiche.prix_demande and elargi != strict:
            _renseigner(fiche, elargi)

        fiches[no_centris] = fiche

    for fiche in fiches.values():
        if fiche.adresse:
            diagnostic.avec_adresse += 1
        if fiche.prix_demande:
            diagnostic.avec_prix += 1
        if fiche.municipalite:
            diagnostic.avec_municipalite += 1
        if fiche.nombre_logements:
            diagnostic.avec_logements += 1

    diagnostic.fiches_uniques += len(fiches)
    return list(fiches.values())


def _decouper_en_blocs(
    texte: str, urls: list[str]
) -> list[tuple[str, str, str, str]]:
    """
    Découpe le courriel en un bloc de texte par fiche.

    Une alerte liste plusieurs immeubles à la file. Le bloc d'une fiche court de
    sa propre position jusqu'à celle de la fiche suivante — jamais au-delà, sinon
    les chiffres du voisin contaminent la lecture.

    Returns:
        [(no_centris, url, bloc_strict, bloc_elargi), ...] dans l'ordre
        d'apparition. Le bloc élargi ne sert qu'en repli.
    """
    reperes: list[tuple[int, str, str]] = []
    vus: set[str] = set()

    for url in urls:
        url = url.rstrip(".,;)")
        no_centris = _no_centris_depuis_url(url)
        if not no_centris or no_centris in vus:
            continue

        # Position du lien dans le texte aplati — `html_vers_texte` a pris soin
        # d'y réinjecter les href.
        position = texte.find(url)
        if position < 0:
            position = texte.find(no_centris)
        if position < 0:
            # Lien invisible dans le texte (courriel purement graphique) :
            # on garde la fiche, sans bloc, plutôt que de la perdre.
            reperes.append((len(texte), no_centris, url))
        else:
            reperes.append((position, no_centris, url))
        vus.add(no_centris)

    reperes.sort(key=lambda r: r[0])

    blocs: list[tuple[str, str, str]] = []
    for index, (position, no_centris, url) in enumerate(reperes):
        if position >= len(texte):
            blocs.append((no_centris, url, ""))
            continue

        suivant = reperes[index + 1][0] if index + 1 < len(reperes) else len(texte)
        fin = min(suivant, position + FENETRE, len(texte))

        # Bloc strict : du lien jusqu'au lien suivant. C'est la mise en page
        # habituelle — vignette cliquable, puis adresse et prix en dessous.
        strict = texte[position:fin]

        # Certaines mises en page placent l'adresse AU-DESSUS du lien. On prévoit
        # donc un bloc élargi vers l'arrière, borné au milieu de l'espace qui
        # sépare des deux fiches : le texte le plus proche d'un lien lui
        # appartient. Il ne sert qu'en repli, si le bloc strict ne donne rien.
        precedent = reperes[index - 1][0] if index > 0 else 0
        plancher = (precedent + position) // 2 if index > 0 else 0
        debut_elargi = max(plancher, position - FENETRE // 3, 0)
        elargi = texte[debut_elargi:fin]

        blocs.append((no_centris, url, strict, elargi))

    return blocs


def _renseigner(fiche: FicheCentris, bloc: str) -> None:
    """Remplit une fiche à partir du texte qui l'entoure."""
    correspondance = MOTIF_ADRESSE.search(bloc)
    if correspondance:
        numero = re.sub(r"\s*[-–]\s*", "-", correspondance.group(1).strip())
        rue = re.sub(r"\s+", " ", correspondance.group(2)).strip(" ,;")
        fiche.adresse = f"{numero} {rue}"

    prix = [p for p in (_nombre(m) for m in MOTIF_PRIX.findall(bloc)) if p]
    if prix:
        # Le PREMIER montant du bloc, pas le plus gros : le prix demandé est
        # l'information mise en avant, alors qu'un maximum attraperait une
        # évaluation municipale ou un montant de la fiche voisine si le
        # découpage a débordé.
        fiche.prix_demande = prix[0]

    logements = MOTIF_LOGEMENTS.search(bloc)
    if logements:
        try:
            fiche.nombre_logements = int(logements.group(1))
        except ValueError:
            pass

    bloc_minuscule = adr.sans_accents(bloc)
    for type_immeuble in TYPES_CENTRIS:
        if adr.sans_accents(type_immeuble) in bloc_minuscule:
            fiche.type_declare = type_immeuble
            break

    # Un plex nomme son nombre de logements.
    if fiche.nombre_logements is None:
        plex = {"duplex": 2, "triplex": 3, "quadruplex": 4, "quintuplex": 5}
        fiche.nombre_logements = plex.get(fiche.type_declare)

    municipalite = MOTIF_MUNICIPALITE.search(bloc)
    if municipalite:
        fiche.municipalite = re.sub(r"\s+", " ", municipalite.group(1)).strip(" -—")
    else:
        # Repli : le segment de l'URL avant le numéro porte souvent la ville.
        segments = fiche.url.split("?")[0].rstrip("/").split("/")
        if len(segments) >= 2:
            candidat = segments[-2]
            if "~" in candidat:
                ville = candidat.split("~")[-1].replace("-", " ").strip()
                if ville and not ville.isdigit():
                    fiche.municipalite = ville.title()


# ─────────────────────────────────────────────────────────────────────────────
# Lecture de la boîte courriel
# ─────────────────────────────────────────────────────────────────────────────


def _corps_message(payload: dict) -> str:
    """Extrait le corps d'un message Gmail, en préférant le HTML au texte."""
    morceaux: list[str] = []

    def parcourir(partie: dict) -> None:
        mime = partie.get("mimeType", "")
        donnees = (partie.get("body") or {}).get("data")
        if donnees and mime in ("text/html", "text/plain"):
            try:
                morceaux.append(
                    base64.urlsafe_b64decode(donnees + "==").decode("utf-8", errors="ignore")
                )
            except Exception:
                pass
        for enfant in partie.get("parts", []) or []:
            parcourir(enfant)

    parcourir(payload)
    return "\n".join(morceaux)


def lire_alertes(jours: int = 7, maximum: int = 50) -> tuple[list[FicheCentris], Diagnostic]:
    """
    Lit les courriels d'alerte Centris de la boîte Gmail et en extrait les fiches.

    Raises:
        RuntimeError: si les identifiants Google ne sont pas configurés.
    """
    from core.auth import get_google_service

    diagnostic = Diagnostic()
    requete = " OR ".join(f"from:{e}" for e in EXPEDITEURS)
    requete = f"({requete}) newer_than:{max(1, jours)}d"

    service = get_google_service("gmail", "v1")
    reponse = (
        service.users()
        .messages()
        .list(userId="me", q=requete, maxResults=maximum)
        .execute()
    )
    messages = reponse.get("messages", [])

    if not messages:
        diagnostic.avertissements.append(
            f"Aucun courriel de {', '.join(EXPEDITEURS)} dans les {jours} derniers jours. "
            "Vérifier que l'alerte Centris est active et que l'expéditeur correspond "
            "à CENTRIS_EXPEDITEURS."
        )
        return [], diagnostic

    toutes: dict[str, FicheCentris] = {}

    for message in messages:
        complet = (
            service.users()
            .messages()
            .get(userId="me", id=message["id"], format="full")
            .execute()
        )
        entetes = {
            h["name"].lower(): h["value"]
            for h in (complet.get("payload") or {}).get("headers", [])
        }
        corps = _corps_message(complet.get("payload") or {})
        diagnostic.courriels_lus += 1

        for fiche in extraire(corps, entetes.get("subject", ""), diagnostic):
            toutes.setdefault(fiche.no_centris, fiche)

    if diagnostic.courriels_lus and not toutes:
        diagnostic.avertissements.append(
            f"{diagnostic.courriels_lus} courriel(s) lu(s) mais AUCUNE fiche extraite. "
            "Le format a probablement changé — lancer `foncier calibrer` pour voir "
            "le texte réellement reçu."
        )

    return list(toutes.values()), diagnostic


def lire_fichier(chemin: str) -> tuple[list[FicheCentris], Diagnostic]:
    """
    Extrait les fiches d'un courriel sauvegardé sur disque (.eml, .html, .txt).

    Sert à calibrer le parseur sans toucher à la boîte courriel.
    """
    diagnostic = Diagnostic()
    with open(chemin, "r", encoding="utf-8", errors="replace") as fichier:
        contenu = fichier.read()

    diagnostic.courriels_lus = 1

    # Un .eml peut porter un corps encodé en quoted-printable ou en base64.
    if chemin.lower().endswith(".eml"):
        contenu = _decoder_eml(contenu)

    fiches = extraire(contenu, f"fichier {os.path.basename(chemin)}", diagnostic)

    if not fiches:
        diagnostic.avertissements.append(
            "Aucune fiche extraite de ce fichier. Le rapport de calibration "
            "ci-dessous montre le texte réellement analysé."
        )

    return fiches, diagnostic


def _decoder_eml(contenu: str) -> str:
    """Décode les corps MIME d'un fichier .eml (quoted-printable, base64)."""
    try:
        import email as email_module
        import email.policy

        message = email_module.message_from_string(
            contenu, policy=email.policy.default
        )
        morceaux = []
        for partie in message.walk():
            if partie.get_content_type() in ("text/html", "text/plain"):
                try:
                    morceaux.append(partie.get_content())
                except Exception:
                    charge = partie.get_payload(decode=True)
                    if charge:
                        morceaux.append(charge.decode("utf-8", errors="replace"))
        if morceaux:
            return "\n".join(morceaux)
    except Exception as e:
        log.warning("foncier.centris: décodage .eml impossible (%s) — texte brut utilisé", e)
    return contenu


# ─────────────────────────────────────────────────────────────────────────────
# Conversion et rapprochement
# ─────────────────────────────────────────────────────────────────────────────


def vers_immeuble(fiche: FicheCentris) -> Optional[Immeuble]:
    """
    Convertit une fiche en `Immeuble`, pour qu'elle entre dans la base.

    Retourne None si la fiche n'a pas d'adresse : sans elle, l'immeuble n'a
    aucune identité rapprochable.
    """
    if not fiche.adresse:
        return None

    immeuble = Immeuble(
        adresse=fiche.adresse,
        municipalite=fiche.municipalite,
        nombre_logements=fiche.nombre_logements,
        valeur_totale=fiche.prix_demande,
        libelle_utilisation=fiche.type_declare or None,
        statut="surveille",
        vu_le=fiche.vu_le,
    )
    immeuble.ajouter_signal(
        Signal(
            cle="mise_en_marche",
            libelle=f"Sur Centris — fiche #{fiche.no_centris}",
            source="centris_alerte",
            date_signal=fiche.vu_le,
            url=fiche.url,
            intensite=1.0,
            donnees={
                "no_centris": fiche.no_centris,
                "prix_demande": fiche.prix_demande,
                "type": fiche.type_declare,
            },
        )
    )
    return immeuble


@dataclass
class Rapprochement:
    """Une fiche Centris reliée à un immeuble déjà suivi."""

    fiche: FicheCentris
    immeuble: Immeuble
    ecart_prix: Optional[float] = None

    def resume(self) -> str:
        # La municipalité du dossier suivi prime sur celle de la fiche : elle
        # vient du rôle d'évaluation, alors que celle de la fiche est déduite
        # d'un fragment d'URL (« montreal-rosemont »).
        ville = self.immeuble.municipalite or self.fiche.municipalite
        lignes = [
            f"🎯 {self.fiche.adresse}, {ville}",
            f"   Suivi depuis {self.immeuble.vu_le or '?'} · score {self.immeuble.score}/100",
        ]
        if self.immeuble.proprietaire:
            lignes.append(f"   Propriétaire : {self.immeuble.proprietaire}")
        if self.immeuble.annees_detention is not None:
            lignes.append(f"   Détention : {self.immeuble.annees_detention} ans")
        if self.fiche.prix_demande and self.immeuble.valeur_totale:
            lignes.append(
                f"   Demandé {self.fiche.prix_demande:,.0f} $ "
                f"vs rôle {self.immeuble.valeur_totale:,.0f} $".replace(",", " ")
                + (f" ({self.ecart_prix:+.0%})" if self.ecart_prix is not None else "")
            )
        lignes.append(f"   {self.fiche.url}")
        return "\n".join(lignes)


def rapprocher(
    fiches: list[FicheCentris], suivis: list[Immeuble]
) -> tuple[list[Rapprochement], list[FicheCentris]]:
    """
    Relie les fiches Centris aux immeubles déjà en base.

    C'est le moment qui vaut le plus cher du système : un immeuble que tu
    surveilles depuis des mois arrive sur le marché, et tu sais déjà qui le
    détient, depuis quand, et ce qu'on peut y construire — pendant que les
    autres découvrent une fiche.

    Returns:
        (rapprochements trouvés, fiches sans correspondance)
    """
    rapprochements: list[Rapprochement] = []
    orphelines: list[FicheCentris] = []

    for fiche in fiches:
        if not fiche.adresse:
            orphelines.append(fiche)
            continue

        trouve = None
        for immeuble in suivis:
            if not immeuble.adresse:
                continue
            if adr.correspond(
                fiche.adresse, fiche.municipalite,
                immeuble.adresse, immeuble.municipalite,
                # Si la municipalité n'a pas pu être extraite de la fiche, on
                # rapproche quand même sur civique + rue.
                exiger_meme_municipalite=bool(fiche.municipalite),
            ):
                trouve = immeuble
                break

        if trouve is None:
            orphelines.append(fiche)
            continue

        ecart = None
        if fiche.prix_demande and trouve.valeur_totale:
            ecart = (fiche.prix_demande - trouve.valeur_totale) / trouve.valeur_totale

        # La mise en marché est un fait à conserver sur le dossier suivi.
        trouve.ajouter_signal(
            Signal(
                cle="mise_en_marche",
                libelle=f"Mis en vente sur Centris — fiche #{fiche.no_centris}",
                source="centris_alerte",
                date_signal=fiche.vu_le,
                url=fiche.url,
                intensite=1.0,
                donnees={"no_centris": fiche.no_centris, "prix_demande": fiche.prix_demande},
            )
        )
        rapprochements.append(Rapprochement(fiche, trouve, ecart))

    log.info(
        "foncier.centris: %d rapprochement(s), %d fiche(s) sans correspondance",
        len(rapprochements), len(orphelines),
    )
    return rapprochements, orphelines


def comparables(fiches: list[FicheCentris], municipalite: str = "") -> str:
    """
    Portrait des prix demandés, par municipalité.

    Ça ne remplace pas de vrais comparables de ventes conclues, mais ça calibre
    les hypothèses de `finance.py` avec des chiffres du marché réel plutôt que
    des moyennes assumées.
    """
    retenues = [
        f for f in fiches
        if f.prix_demande and (
            not municipalite
            or adr.normaliser_municipalite(f.municipalite)
            == adr.normaliser_municipalite(municipalite)
        )
    ]
    if not retenues:
        return "Aucune fiche avec prix pour ce territoire."

    par_ville: dict[str, list[float]] = {}
    for fiche in retenues:
        par_ville.setdefault(fiche.municipalite or "?", []).append(fiche.prix_demande)

    lignes = [f"Prix demandés — {len(retenues)} fiche(s)"]
    for ville, prix in sorted(par_ville.items(), key=lambda kv: -len(kv[1])):
        lignes.append(
            f"  {ville} — {len(prix)} fiche(s) · "
            f"médiane {_mediane(prix):,.0f} $ · "
            f"de {min(prix):,.0f} $ à {max(prix):,.0f} $".replace(",", " ")
        )

    # Prix par logement, la mesure qui se compare vraiment entre immeubles.
    par_logement = [
        f.prix_demande / f.nombre_logements
        for f in retenues
        if f.nombre_logements and f.nombre_logements > 0
    ]
    if par_logement:
        lignes.append("")
        lignes.append(
            f"  Prix par logement — médiane "
            f"{_mediane(par_logement):,.0f} $ "
            f"(sur {len(par_logement)} fiche(s))".replace(",", " ")
        )

    return "\n".join(lignes)


def _mediane(valeurs: list[float]) -> float:
    """Médiane réelle — moyenne des deux valeurs centrales si le compte est pair."""
    tri = sorted(valeurs)
    milieu = len(tri) // 2
    if len(tri) % 2:
        return tri[milieu]
    return (tri[milieu - 1] + tri[milieu]) / 2


def borne_periode(jours: int) -> tuple[str, str]:
    """Bornes lisibles de la période analysée."""
    fin = datetime.now().date()
    return (fin - timedelta(days=jours)).isoformat(), fin.isoformat()
