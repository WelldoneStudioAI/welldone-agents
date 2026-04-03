"""
agents/email.py — Agent email WHC avec triage décisionnel IA

Architecture en 4 couches :
  1. Ingestion IMAP (headers rapides)
  2. Pré-filtrage dur (heuristiques sans IA)
  3. Classification LLM par lot (GPT-4o)
  4. Sortie orientée-action (P1/P2 seulement)

Commandes :
  trier          → Analyse intelligente, filtre le bruit, montre ce qui mérite attention
  lire           → Liste brute des N derniers emails
  chercher       → Recherche par expéditeur / sujet / mot-clé
  résumer        → Lit un email complet + résumé GPT
  rédiger        → GPT rédige un email à partir d'instructions
  envoyer        → Envoie via SMTP
  filtres        → Liste les filtres actifs
  créer_filtre   → Crée une règle de tri automatique
  appliquer_filtres → Applique les filtres sur la boîte maintenant
  dossiers       → Liste les dossiers IMAP

Config Railway :
  WHC_IMAP_HOST, WHC_SMTP_HOST, WHC_EMAIL, WHC_PASSWORD
"""

import asyncio, email, imaplib, json, logging, os, re, smtplib, ssl, textwrap
from email.header import decode_header as _decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path

from agents._base import BaseAgent

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
IMAP_HOST = os.environ.get("WHC_IMAP_HOST", "mail.awelldone.com")
IMAP_PORT = int(os.environ.get("WHC_IMAP_PORT", "993"))
SMTP_HOST = os.environ.get("WHC_SMTP_HOST", "mail.awelldone.com")
SMTP_PORT = int(os.environ.get("WHC_SMTP_PORT", "465"))
WHC_EMAIL = os.environ.get("WHC_EMAIL", "jptanguay@awelldone.com")
WHC_PASS  = os.environ.get("WHC_PASSWORD", "")

# ── Hostinger (boîte principale jptanguay@awelldone.com) ─────────────────────
HST_IMAP_HOST = os.environ.get("HST_IMAP_HOST", "imap.hostinger.com")
HST_IMAP_PORT = int(os.environ.get("HST_IMAP_PORT", "993"))
HST_EMAIL     = os.environ.get("HST_EMAIL", "jptanguay@awelldone.com")
HST_PASS      = os.environ.get("HST_PASSWORD", "")

# Toutes les boîtes à surveiller (host, port, user, pass, label)
_ALL_ACCOUNTS = [
    (IMAP_HOST, IMAP_PORT, WHC_EMAIL, WHC_PASS,  "WHC"),
    (HST_IMAP_HOST, HST_IMAP_PORT, HST_EMAIL, HST_PASS, "Hostinger"),
]

# ── Persistance filtres sur disque ────────────────────────────────────────────
_FILTERS_FILE = Path.home() / ".welldone" / "email_filters.json"
_SENDER_MEMORY_FILE  = Path.home() / ".welldone" / "email_sender_memory.json"
_PROCESSED_UIDS_FILE = Path.home() / ".welldone" / "email_processed_uids.json"

def _load_processed_uids() -> dict:
    try:
        if _PROCESSED_UIDS_FILE.exists():
            raw = json.loads(_PROCESSED_UIDS_FILE.read_text())
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            return {acct: {uid: d for uid, d in uids.items() if d >= cutoff}
                    for acct, uids in raw.items()}
    except Exception as e:
        log.warning(f"email: erreur chargement processed UIDs: {e}")
    return {}

def _save_processed_uids(processed: dict) -> None:
    try:
        _PROCESSED_UIDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROCESSED_UIDS_FILE.write_text(json.dumps(processed, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"email: erreur sauvegarde processed UIDs: {e}")

def _load_filters() -> list[dict]:
    try:
        _FILTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _FILTERS_FILE.exists():
            return json.loads(_FILTERS_FILE.read_text())
    except Exception as e:
        log.warning(f"email: erreur chargement filtres: {e}")
    return []

def _save_filters(filters: list[dict]) -> None:
    try:
        _FILTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILTERS_FILE.write_text(json.dumps(filters, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"email: erreur sauvegarde filtres: {e}")

def _load_sender_memory() -> dict:
    try:
        if _SENDER_MEMORY_FILE.exists():
            return json.loads(_SENDER_MEMORY_FILE.read_text())
    except Exception as e:
        log.warning(f"email: erreur chargement mémoire expéditeurs: {e}")
    return {}

def _save_sender_memory(memory: dict) -> None:
    try:
        _SENDER_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SENDER_MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"email: erreur sauvegarde mémoire expéditeurs: {e}")

# ── Mémoire expéditeurs (persistée sur disque) ────────────────────────────────
# Structure : { "email@domain.com": { "type": "CLIENT_ACTIF", "bias": 18, "notes": "" } }
_SENDER_MEMORY: dict[str, dict] = _load_sender_memory()

# ── Filtres locaux (persistés sur disque) ─────────────────────────────────────
# Structure : [ { "id": "...", "name": "...", "conditions": [...], "actions": [...] } ]
_FILTERS: list[dict] = _load_filters()
log.info(f"email: {len(_FILTERS)} filtre(s) chargé(s) depuis {_FILTERS_FILE}")

# ── Whitelist contacts connus (persistée sur disque) ──────────────────────────
_WHITELIST_FILE = Path.home() / ".welldone" / "email_whitelist.json"

def _load_whitelist() -> set[str]:
    try:
        if _WHITELIST_FILE.exists():
            data = json.loads(_WHITELIST_FILE.read_text())
            return set(data.get("emails", []))
    except Exception as e:
        log.warning(f"email: erreur chargement whitelist: {e}")
    return set()

def _save_whitelist(emails: set[str]) -> None:
    try:
        _WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WHITELIST_FILE.write_text(json.dumps(
            {"emails": sorted(emails), "count": len(emails)},
            ensure_ascii=False, indent=2
        ))
    except Exception as e:
        log.warning(f"email: erreur sauvegarde whitelist: {e}")

_KNOWN_CONTACTS: set[str] = _load_whitelist()
log.info(f"email: {len(_KNOWN_CONTACTS)} contacts connus chargés")

# ── Catégories & priorités ────────────────────────────────────────────────────
CATEGORIES = [
    "CLIENT_ACTIF", "PROSPECT_CHAUD", "PROSPECT_FROID", "PARTENAIRE_COLLAB",
    "FACTURATION_PAIEMENT", "CONTRAT_LEGAL", "ADMIN_OPERATIONNEL",
    "SUPPORT_TECHNIQUE", "RENDEZ_VOUS_ECHEANCE", "NEWSLETTER_PROMO",
    "SPAM_BRUIT", "PERSONNEL_AUTRE",
]
PRIORITY_LABELS = {
    "P1_CRITIQUE": "🔴 P1",
    "P2_IMPORTANT": "🟠 P2",
    "P3_UTILE_NON_URGENT": "🟡 P3",
    "P4_ARCHIVE_BRUIT": "⚪ P4",
}
URGENCY_LABELS = {
    "aujourd_hui": "⚡ Aujourd'hui",
    "cette_semaine": "📅 Cette semaine",
    "quand_possible": "🕐 Quand possible",
    "aucune": "",
}

# ── Heuristiques de pré-filtrage ──────────────────────────────────────────────
BULK_HEADERS = ["list-unsubscribe", "list-id", "precedence"]
BULK_SENDERS = ["newsletter@", "no-reply@", "noreply@", "updates@", "marketing@",
                "notifications@", "donotreply@", "info@mailchimp", "bounce@"]
BULK_SUBJECT_WORDS = [
    "sale", "offer", "save now", "deals", "newsletter", "digest",
    "weekly update", "industry report", "product news", "unsubscribe",
    "% off", "free shipping", "limited time", "click here",
]

# ── Prompt système triage ─────────────────────────────────────────────────────
TRIAGE_SYSTEM_PROMPT = """Tu es un agent de tri de courriels professionnel pour Jean-Philippe Roy,
fondateur de Welldone Studio à Montréal (photographie, vidéo, branding, design, immobilier, architecture,
restauration, hôtellerie, clients PME au Québec).

Ta mission n'est PAS de résumer toute la boîte mail.
Ta mission est d'identifier UNIQUEMENT les messages réellement importants.

Classifie chaque courriel dans UNE seule catégorie :
CLIENT_ACTIF, PROSPECT_CHAUD, PROSPECT_FROID, PARTENAIRE_COLLAB, FACTURATION_PAIEMENT,
CONTRAT_LEGAL, ADMIN_OPERATIONNEL, SUPPORT_TECHNIQUE, RENDEZ_VOUS_ECHEANCE,
NEWSLETTER_PROMO, SPAM_BRUIT, PERSONNEL_AUTRE

Attribue :
- score : 0-100 (signaux positifs/négatifs détaillés ci-dessous)
- priority : P1_CRITIQUE (80-100) | P2_IMPORTANT (60-79) | P3_UTILE_NON_URGENT (40-59) | P4_ARCHIVE_BRUIT (0-39)
- action_required : true/false
- urgency : aujourd_hui | cette_semaine | quand_possible | aucune
- recommended_action : phrase courte et concrète
- why_important : 1-2 phrases de justification

SIGNAUX POSITIFS :
+25 expéditeur client actif connu
+20 mention facture/paiement/dépôt/devis/soumission
+20 mention contrat/NDA/document à signer
+20 demande claire nécessitant une réponse humaine
+18 nouveau prospect crédible, personnalisé
+18 demande de prix / disponibilité / appel / rencontre
+15 problème bloquant ou compte bloqué
+15 décision à prendre / urgence explicite
+15 thread existant pertinent
+12 question explicite dans le corps
+12 réunion ou rendez-vous mentionné
+10 opportunité alignée avec photographie/vidéo/branding/design/immobilier
+8 contact local Québec / Montréal crédible
+5 pièce jointe contractuelle ou utile

SIGNAUX NÉGATIFS :
-40 présence de "unsubscribe", "view in browser", "manage preferences"
-30 expéditeur bulk/newsletter/no-reply évident
-30 marketing automation évident
-25 sujet promotionnel générique (sale, offer, deals, % off)
-25 message de voyage / shopping / marketing sans lien avec le travail
-20 offre générique non sollicitée
-15 contenu informatif sans action attendue
-10 répétition d'un contenu de plateforme

RÈGLE ABSOLUE : NEWSLETTER_PROMO et SPAM_BRUIT ne peuvent JAMAIS être P1_CRITIQUE.

Retourne UNIQUEMENT un JSON valide dans ce format exact :
{
  "results": [
    {
      "uid": "string",
      "category": "...",
      "priority": "...",
      "score": 0,
      "action_required": false,
      "urgency": "aucune",
      "recommended_action": "...",
      "why_important": "..."
    }
  ]
}"""

TRIAGE_BATCH_PROMPT = """Analyse ces emails selon la grille :
1. Pré-filtre le bruit et newsletters.
2. Classe chaque email dans une catégorie unique.
3. Score sur 100, priorité P1-P4, action requise, action recommandée.
4. Ne retourne QUE P1 et P2 dans les résultats principaux.
5. Compte les exclusions.

Emails à analyser :
{emails_json}

Retourne le JSON demandé. Ne retourne PAS de texte en dehors du JSON."""


# ── Helpers IMAP ──────────────────────────────────────────────────────────────

def _decode(s) -> str:
    if s is None:
        return ""
    try:
        parts = _decode_header(s if isinstance(s, str) else s.decode("utf-8", errors="replace"))
        result = []
        for part, enc in parts:
            if isinstance(part, bytes):
                result.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return "".join(result)
    except Exception:
        return str(s)


def _connect() -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    M.login(WHC_EMAIL, WHC_PASS)
    return M


def _connect_account(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL | None:
    """Connexion IMAP générique — retourne None si échec (boîte non configurée)."""
    if not password:
        return None
    try:
        ctx = ssl.create_default_context()
        M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        M.login(user, password)
        return M
    except Exception as e:
        log.warning(f"email: impossible de se connecter à {host} ({user}): {e}")
        return None


def _fetch_inbox_emails(host: str, port: int, user: str, password: str,
                        label: str, limit: int, unseen_only: bool) -> list[dict]:
    """Récupère les emails d'un compte IMAP et les retourne avec le label du compte."""
    M = _connect_account(host, port, user, password)
    if M is None:
        return []
    try:
        M.select("INBOX")
        if unseen_only:
            typ, data = M.search(None, "UNSEEN")
        else:
            typ, data = M.search(None, "ALL")
        all_ids = data[0].split() if data[0] else []
        target  = all_ids[-limit:]
        results = []
        for uid in reversed(target):
            uid_s = _uid_str(uid)
            try:
                typ2, msg_data = M.fetch(uid_s, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                from_raw  = _decode(msg.get("From", ""))
                subject   = _decode(msg.get("Subject", "(sans objet)"))
                date_str  = _parse_date(msg.get("Date", ""))
                headers_raw = raw.decode("utf-8", errors="replace")[:2000]
                from_match = re.search(r"<([^>]+)>", from_raw)
                from_email = from_match.group(1).lower() if from_match else from_raw.lower()
                is_bulk = (
                    _is_bulk_by_headers(headers_raw)
                    or _is_bulk_by_sender(from_raw)
                    or _is_bulk_by_subject(subject)
                )
                snippet     = _get_body_snippet(msg, 500) if not is_bulk else ""
                attachments = _get_attachments(msg)
                results.append({
                    "uid":              uid_s,
                    "account":          label,
                    "from":             from_raw,
                    "from_email":       from_email,
                    "subject":          subject,
                    "date":             date_str,
                    "snippet":          snippet,
                    "has_attachment":   bool(attachments),
                    "attachment_names": attachments,
                    "pre_filtered":     is_bulk,
                })
            except Exception as e:
                log.warning(f"Erreur fetch UID {uid_s} ({label}): {e}")
        return results
    except Exception as e:
        log.warning(f"email: erreur lecture INBOX {label}: {e}")
        return []
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _parse_date(date_str: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%d %b %Y %H:%M")
    except Exception:
        return date_str or "?"


def _get_body_snippet(msg, max_chars: int = 600) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    continue
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            body = str(msg.get_payload())

    # Nettoyer HTML et whitespace
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:max_chars]


def _get_attachments(msg) -> list[str]:
    attachments = []
    for part in msg.walk():
        cd = str(part.get("Content-Disposition", ""))
        if "attachment" in cd:
            filename = _decode(part.get_filename() or "")
            if filename:
                attachments.append(filename)
    return attachments


def _uid_str(uid) -> str:
    return uid.decode() if isinstance(uid, bytes) else str(uid)


# ── Pré-filtrage heuristique ──────────────────────────────────────────────────

def _is_bulk_by_headers(raw_headers: str) -> bool:
    """Retourne True si les headers indiquent un email bulk/newsletter."""
    lower = raw_headers.lower()
    for h in BULK_HEADERS:
        if h in lower:
            return True
    return False


def _is_bulk_by_sender(from_str: str) -> bool:
    lower = from_str.lower()
    for pattern in BULK_SENDERS:
        if pattern in lower:
            return True
    return False


def _is_bulk_by_subject(subject: str) -> bool:
    lower = subject.lower()
    for word in BULK_SUBJECT_WORDS:
        if word in lower:
            return True
    return False


def _apply_sender_bias(from_email: str, base_score: int) -> int:
    """Ajuste le score en fonction de la mémoire expéditeur."""
    mem = _SENDER_MEMORY.get(from_email.lower(), {})
    bias = mem.get("bias", 0)
    sender_type = mem.get("type", "")
    if sender_type in ("NEWSLETTER", "BRUIT"):
        bias = min(bias, -30)
    elif sender_type in ("CLIENT_ACTIF", "PARTENAIRE"):
        bias = max(bias, 15)
    return max(0, min(100, base_score + bias))


# ── Classification GPT par lot ────────────────────────────────────────────────

def _gpt_triage_batch(emails: list[dict]) -> list[dict]:
    """Envoie un lot d'emails à GPT-4o pour classification. Retourne les résultats."""
    try:
        import openai
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

        emails_text = json.dumps([{
            "uid":     e["uid"],
            "from":    e["from"],
            "subject": e["subject"],
            "date":    e["date"],
            "snippet": e.get("snippet", ""),
            "has_attachment": e.get("has_attachment", False),
        } for e in emails], ensure_ascii=False, indent=2)

        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user",   "content": TRIAGE_BATCH_PROMPT.format(emails_json=emails_text)},
            ],
            max_tokens=3000,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        return data.get("results", [])
    except Exception as e:
        log.error(f"GPT triage error: {e}")
        return []


def _gpt_draft(to: str, instructions: str) -> str:
    """GPT-4o rédige un email professionnel."""
    try:
        import openai
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": (
                    "Tu es l'assistant email de Jean-Philippe Roy, fondateur de Welldone Studio Montréal. "
                    "Tu rédiges des emails professionnels, directs et chaleureux en français "
                    "(anglais si le contexte l'exige). "
                    "Format : première ligne = 'Sujet: [sujet concis]', puis une ligne vide, puis le corps."
                )},
                {"role": "user", "content": f"Destinataire: {to}\nInstructions: {instructions}"},
            ],
            max_tokens=800,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Erreur GPT: {e}"


# ── Agent ──────────────────────────────────────────────────────────────────────

_ARCHIVES_FOLDER = "INBOX.Archives"

# Mots français courants pour détecter les emails en français direct
_FR_WORDS = {"bonjour","bonsoir","salut","merci","svp","s'il","voici","votre","votre","nous","vous",
             "est","sont","pour","avec","dans","mais","qui","que","une","les","des","plus","bien",
             "comme","aussi","tout","cette","votre","notre","avoir","faire","être","ici","donc"}


def _is_direct_french(msg) -> bool:
    """Retourne True si l'email semble être un message direct en français (pas bulk)."""
    # Rejeter si bulk headers présents
    for h in BULK_HEADERS:
        if msg.get(h):
            return False
    # Rejeter si expéditeur bulk évident
    from_raw = _decode(msg.get("From", "")).lower()
    if _is_bulk_by_sender(from_raw):
        return False
    # Détecter le français dans le corps
    body = _get_body_snippet(msg, 500).lower()
    body_words = set(re.findall(r'\b[a-zàâéèêëîïôùûüç]{3,}\b', body))
    fr_match = len(body_words & _FR_WORDS)
    return fr_match >= 3


class EmailAgent(BaseAgent):
    name        = "email"
    description = "Boîte WHC (Courriels) avec triage auto horaire — whitelist + français direct → INBOX, reste → Archives"

    schedules = [
        {
            "id":     "email_auto_trier",
            "cron":   "0 * * * *",   # chaque heure
            "command": "auto_trier",
            "context": {},
            "label":  "Triage email horaire",
        }
    ]

    @property
    def commands(self):
        return {
            "trier":                self.trier,
            "lire":                 self.lire,
            "chercher":             self.chercher,
            "résumer":              self.resumer,
            "rédiger":              self.rediger,
            "envoyer":              self.envoyer,
            "filtres":              self.mes_filtres,
            "créer_filtre":         self.creer_filtre,
            "appliquer_filtres":    self.appliquer_filtres,
            "dossiers":             self.dossiers,
            "construire_whitelist": self.construire_whitelist,
            "trier_boite":          self.trier_boite,
            "auto_trier":           self.auto_trier,
        }

    # ── TRIER — commande principale ───────────────────────────────────────────

    async def trier(self, ctx: dict | None = None) -> str:
        """
        Triage intelligent : analyse les N derniers emails, pré-filtre le bruit,
        classe avec GPT, retourne seulement P1/P2 avec actions recommandées.
        """
        ctx     = ctx or {}
        limit   = int(ctx.get("limit", 50))
        mode    = ctx.get("mode", "INBOX_IMPORTANTE")  # voir modes ci-dessous
        unseen_only = ctx.get("unseen_only", False)

        def _fetch_emails():
            all_emails = []
            for host, port, user, password, label in _ALL_ACCOUNTS:
                emails = _fetch_inbox_emails(host, port, user, password, label, limit, unseen_only)
                all_emails.extend(emails)
            # Trier par date décroissante (best effort — date est une string formatée)
            return all_emails

        try:
            all_emails = await asyncio.get_event_loop().run_in_executor(None, _fetch_emails)
        except Exception as e:
            return f"❌ Erreur IMAP: {e}"

        if not all_emails:
            return "📭 Aucun email à analyser."

        # Séparer pré-filtrés vs à analyser
        to_analyze  = [e for e in all_emails if not e["pre_filtered"]]
        pre_filtered = [e for e in all_emails if e["pre_filtered"]]

        # Classification GPT sur les emails non pré-filtrés
        gpt_results: list[dict] = []
        if to_analyze:
            gpt_results = await asyncio.get_event_loop().run_in_executor(
                None, _gpt_triage_batch, to_analyze
            )

        # Fusionner résultats GPT avec données brutes
        uid_to_raw  = {e["uid"]: e for e in to_analyze}
        uid_to_gpt  = {r["uid"]: r for r in gpt_results}

        scored = []
        for uid, raw in uid_to_raw.items():
            gpt = uid_to_gpt.get(uid, {})
            score = int(gpt.get("score", 0))
            # Ajuster avec mémoire expéditeur
            score = _apply_sender_bias(raw["from_email"], score)
            priority = gpt.get("priority", "P4_ARCHIVE_BRUIT")
            # Re-mapper si score ajusté change la priorité
            if score >= 80:
                priority = "P1_CRITIQUE"
            elif score >= 60:
                priority = "P2_IMPORTANT"
            elif score >= 40:
                priority = "P3_UTILE_NON_URGENT"
            else:
                priority = "P4_ARCHIVE_BRUIT"

            scored.append({
                **raw,
                "score":    score,
                "priority": priority,
                "category": gpt.get("category", "PERSONNEL_AUTRE"),
                "action_required": gpt.get("action_required", False),
                "urgency":  gpt.get("urgency", "aucune"),
                "recommended_action": gpt.get("recommended_action", ""),
                "why_important": gpt.get("why_important", ""),
            })

        # Filtrer selon le mode
        if mode == "REPONSES_A_FAIRE":
            show = [e for e in scored if e["action_required"] and e["priority"] in ("P1_CRITIQUE", "P2_IMPORTANT")]
        elif mode == "ARGENT_ADMIN":
            show = [e for e in scored if e["category"] in ("FACTURATION_PAIEMENT", "CONTRAT_LEGAL", "ADMIN_OPERATIONNEL")]
        elif mode == "OPPORTUNITES":
            show = [e for e in scored if e["category"] in ("PROSPECT_CHAUD", "PARTENAIRE_COLLAB", "CLIENT_ACTIF")]
        elif mode == "NETTOYAGE":
            show = [e for e in scored if e["priority"] == "P4_ARCHIVE_BRUIT"]
        else:  # INBOX_IMPORTANTE (défaut)
            show = [e for e in scored if e["priority"] in ("P1_CRITIQUE", "P2_IMPORTANT")]

        # Trier par score décroissant
        show.sort(key=lambda x: x["score"], reverse=True)

        p3_count = len([e for e in scored if e["priority"] == "P3_UTILE_NON_URGENT"])
        p4_count = len([e for e in scored if e["priority"] == "P4_ARCHIVE_BRUIT"]) + len(pre_filtered)
        p1_count = len([e for e in show if e["priority"] == "P1_CRITIQUE"])
        p2_count = len([e for e in show if e["priority"] == "P2_IMPORTANT"])

        # ── Construire la réponse ──────────────────────────────────────────────
        lines = [
            f"📊 *Analyse de {len(all_emails)} emails*\n"
            f"└ {p4_count} exclus (bruit/promos) · {p3_count} utiles non urgents · "
            f"*{len(show)} requièrent attention* ({p1_count} critiques, {p2_count} importants)\n"
        ]

        if not show:
            lines.append("✅ *Rien d'urgent dans la boîte.* Tu es à jour !")
            if p3_count:
                lines.append(f"\n_{p3_count} messages P3 (utiles mais non urgents) — tape 'emails P3' pour voir._")
            return "\n".join(lines)

        # Séparer par section
        critical = [e for e in show if e["priority"] == "P1_CRITIQUE"]
        important = [e for e in show if e["priority"] == "P2_IMPORTANT"]
        opportunites = [e for e in show if e["category"] in ("PROSPECT_CHAUD", "PARTENAIRE_COLLAB")]
        admin = [e for e in show if e["category"] in ("FACTURATION_PAIEMENT", "CONTRAT_LEGAL", "ADMIN_OPERATIONNEL")]

        def fmt_email(e: dict, idx: int) -> str:
            prio = PRIORITY_LABELS.get(e["priority"], e["priority"])
            urgency = URGENCY_LABELS.get(e["urgency"], "")
            urgency_str = f" · {urgency}" if urgency else ""
            cat = e["category"].replace("_", " ").title()
            action = f"\n   → *{e['recommended_action']}*" if e["recommended_action"] else ""
            why = f"\n   _{e['why_important']}_" if e["why_important"] else ""
            pj = f" 📎" if e.get("has_attachment") else ""
            return (
                f"*{idx}. {prio} [{e['uid']}]{pj}* — {e['score']}/100{urgency_str}\n"
                f"   De: {e['from']}\n"
                f"   Sujet: {e['subject']}\n"
                f"   Catégorie: `{cat}`"
                f"{why}"
                f"{action}"
            )

        idx = 1
        if critical:
            lines.append("🔴 *À TRAITER MAINTENANT*\n")
            for e in critical:
                lines.append(fmt_email(e, idx))
                lines.append("")
                idx += 1

        if important:
            lines.append("🟠 *IMPORTANT — cette semaine*\n")
            for e in important:
                lines.append(fmt_email(e, idx))
                lines.append("")
                idx += 1

        lines.append(
            f"_{p4_count} messages exclus comme bruit, promos ou faible priorité._\n"
            f"_Tape `/email résumer [uid]` pour lire un message complet._"
        )

        return "\n".join(lines)

    # ── LIRE — liste brute ────────────────────────────────────────────────────

    async def lire(self, ctx: dict | None = None) -> str:
        ctx   = ctx or {}
        limit = int(ctx.get("limit", 15))

        def _run():
            M = _connect()
            M.select("INBOX")
            typ, data = M.search(None, "ALL")
            all_ids = data[0].split() if data[0] else []
            target  = list(reversed(all_ids[-limit:]))

            if not target:
                M.logout()
                return []

            results = []
            for uid in target:
                uid_s = _uid_str(uid)
                try:
                    typ2, msg_data = M.fetch(uid_s, "(RFC822.HEADER)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    results.append({
                        "uid":     uid_s,
                        "from":    _decode(msg.get("From", "?")),
                        "subject": _decode(msg.get("Subject", "(sans objet)")),
                        "date":    _parse_date(msg.get("Date", "")),
                    })
                except Exception:
                    continue
            M.logout()
            return results

        try:
            items = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            return f"❌ Erreur IMAP: {e}"

        if not items:
            return "📭 Boîte vide."

        lines = [f"📬 *{len(items)} derniers emails* (boîte WHC)\n"]
        for it in items:
            lines.append(f"*[{it['uid']}]* {it['date']}\nDe: {it['from']}\nSujet: {it['subject']}\n")
        lines.append("_`/email résumer [uid]` pour lire · `/email trier` pour le triage IA_")
        return "\n".join(lines)

    # ── CHERCHER ──────────────────────────────────────────────────────────────

    async def chercher(self, ctx: dict | None = None) -> str:
        ctx   = ctx or {}
        query = ctx.get("query", "").strip()
        if not query:
            return "❌ Précise ce que tu cherches."

        def _run():
            M = _connect()
            M.select("INBOX")
            results = set()
            for criterion in [f'SUBJECT "{query}"', f'FROM "{query}"', f'TEXT "{query}"']:
                try:
                    typ, data = M.search(None, criterion)
                    if data[0]:
                        for uid in data[0].split():
                            results.add(_uid_str(uid))
                except Exception:
                    pass
            if not results:
                M.logout()
                return []
            sorted_ids = sorted(results, key=lambda x: int(x), reverse=True)[:20]
            items = []
            for uid_s in sorted_ids:
                try:
                    typ2, msg_data = M.fetch(uid_s, "(RFC822.HEADER)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    items.append({
                        "uid":     uid_s,
                        "from":    _decode(msg.get("From", "?")),
                        "subject": _decode(msg.get("Subject", "(sans objet)")),
                        "date":    _parse_date(msg.get("Date", "")),
                    })
                except Exception:
                    continue
            M.logout()
            return items

        try:
            items = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            return f"❌ Erreur recherche: {e}"

        if not items:
            return f"🔍 Aucun email trouvé pour *{query}*."

        lines = [f"🔍 *{len(items)} résultat(s)* pour *{query}*:\n"]
        for it in items:
            lines.append(f"*[{it['uid']}]* {it['date']}\nDe: {it['from']}\nSujet: {it['subject']}\n")
        lines.append("_`/email résumer [uid]` pour lire un email complet._")
        return "\n".join(lines)

    # ── RÉSUMER ───────────────────────────────────────────────────────────────

    async def resumer(self, ctx: dict | None = None) -> str:
        ctx = ctx or {}
        uid = str(ctx.get("uid", "")).strip()
        if not uid:
            return "❌ Précise l'UID. Ex: `/email résumer 12345`"

        def _run():
            M = _connect()
            M.select("INBOX")
            typ, data = M.fetch(uid, "(RFC822)")
            if not data or data[0] is None:
                M.logout()
                return None
            raw = data[0][1]
            msg = email.message_from_bytes(raw)
            meta = {
                "from":    _decode(msg.get("From", "?")),
                "to":      _decode(msg.get("To", "?")),
                "subject": _decode(msg.get("Subject", "(sans objet)")),
                "date":    _parse_date(msg.get("Date", "")),
            }
            body        = _get_body_snippet(msg, 4000)
            attachments = _get_attachments(msg)
            M.store(uid, "+FLAGS", "\\Seen")
            M.logout()
            return meta, body, attachments

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            return f"❌ Erreur lecture: {e}"

        if result is None:
            return f"❌ Email [{uid}] introuvable."

        meta, body, attachments = result

        # Triage + résumé GPT en un appel
        def _analyze():
            import openai
            client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": (
                        "Tu es l'assistant email de Jean-Philippe Roy (Welldone Studio, Montréal). "
                        "Réponds en français, de façon concise et professionnelle."
                    )},
                    {"role": "user", "content": (
                        f"Email de: {meta['from']}\nSujet: {meta['subject']}\nDate: {meta['date']}\n\n{body}\n\n"
                        "Résume en 3-5 lignes : qui écrit, la demande principale, le ton, et si une action est requise. "
                        "Puis propose une action concrète recommandée."
                    )},
                ],
                max_tokens=400,
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()

        summary = await asyncio.get_event_loop().run_in_executor(None, _analyze)

        pj_str = f"\n📎 *{len(attachments)} PJ* : {', '.join(attachments)}" if attachments else ""
        preview = textwrap.shorten(body, width=800, placeholder="…")

        return (
            f"📧 *Email [{uid}]*\n"
            f"De: {meta['from']}\n"
            f"À: {meta['to']}\n"
            f"Sujet: {meta['subject']}\n"
            f"Date: {meta['date']}"
            f"{pj_str}\n\n"
            f"*Analyse IA :*\n{summary}\n\n"
            f"*Contenu :*\n{preview}"
        )

    # ── RÉDIGER ───────────────────────────────────────────────────────────────

    async def rediger(self, ctx: dict | None = None) -> str:
        ctx          = ctx or {}
        to           = ctx.get("to", "")
        instructions = ctx.get("contexte", ctx.get("instructions", ""))
        if not instructions:
            return "❌ Décris ce que tu veux écrire."

        draft = await asyncio.get_event_loop().run_in_executor(
            None, _gpt_draft, to, instructions
        )
        lines = draft.split("\n")
        subject, body_lines = "", []
        for line in lines:
            if line.startswith("Sujet:") and not subject:
                subject = line.replace("Sujet:", "").strip()
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()

        return (
            f"✍️ *Brouillon rédigé*\n\n"
            f"*À:* {to or '(à préciser)'}\n"
            f"*Sujet:* {subject}\n\n"
            f"{body}\n\n"
            f"---\n"
            f"_Dis-moi 'envoie cet email' pour l'envoyer ou 'modifie...' pour ajuster._"
        )

    # ── ENVOYER ───────────────────────────────────────────────────────────────

    async def envoyer(self, ctx: dict | None = None) -> str:
        ctx     = ctx or {}
        to      = ctx.get("to", "")
        subject = ctx.get("subject", "")
        body    = ctx.get("body", "")
        if not all([to, subject, body]):
            return "❌ Champs manquants : to, subject, body."

        def _send():
            msg = MIMEMultipart("alternative")
            msg["From"]    = WHC_EMAIL
            msg["To"]      = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            ctx_ssl = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx_ssl) as server:
                server.login(WHC_EMAIL, WHC_PASS)
                server.sendmail(WHC_EMAIL, to, msg.as_string())

        try:
            await asyncio.get_event_loop().run_in_executor(None, _send)
            return f"✅ *Email envoyé !*\nÀ: {to}\nSujet: {subject}"
        except Exception as e:
            return f"❌ Erreur SMTP: {e}"

    # ── DOSSIERS ──────────────────────────────────────────────────────────────

    async def dossiers(self, ctx: dict | None = None) -> str:
        def _run():
            M = _connect()
            typ, folders = M.list()
            M.logout()
            result = []
            for f in folders:
                if f and f != b")":
                    decoded = f.decode("utf-8", errors="replace") if isinstance(f, bytes) else str(f)
                    # Extraire le nom du dossier (après le dernier "." ou espace)
                    parts = decoded.split('"."')
                    name = parts[-1].strip().strip('"') if len(parts) > 1 else decoded
                    result.append(name)
            return result

        try:
            folders = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            return f"❌ Erreur: {e}"

        lines = ["📁 *Dossiers IMAP (boîte WHC)*\n"]
        for f in folders:
            lines.append(f"• `{f}`")
        lines.append("\n_Tu peux créer des filtres pour trier automatiquement dans ces dossiers._")
        return "\n".join(lines)

    # ── WHITELIST + TRI RAPIDE ────────────────────────────────────────────────

    async def construire_whitelist(self, ctx: dict | None = None) -> str:
        """
        Scanne le dossier Sent → extrait tous les destinataires → whitelist persistée.
        À lancer une fois. Met à jour la liste à chaque appel.
        """
        global _KNOWN_CONTACTS

        def _scan_sent():
            M = _connect()
            contacts = set()
            for folder in ["INBOX.Sent", "Sent", "Sent Messages", "INBOX.Sent Messages"]:
                try:
                    typ, _ = M.select(folder, readonly=True)
                    if typ != "OK":
                        continue
                    typ2, data = M.search(None, "ALL")
                    ids = data[0].split() if data[0] else []
                    # Traiter par lots de 100
                    for i in range(0, len(ids), 100):
                        batch = ids[i:i+100]
                        id_list = ",".join(i.decode() for i in batch)
                        try:
                            typ3, msgs = M.fetch(id_list, "(RFC822.HEADER)")
                            for item in msgs:
                                if not isinstance(item, tuple):
                                    continue
                                msg = email.message_from_bytes(item[1])
                                for header in ["To", "Cc"]:
                                    raw = msg.get(header, "")
                                    if not raw:
                                        continue
                                    for addr in raw.split(","):
                                        m = re.search(r"<([^>]+)>", addr)
                                        e = m.group(1).lower() if m else addr.strip().lower()
                                        if "@" in e and len(e) < 100:
                                            contacts.add(e)
                        except Exception:
                            continue
                    M.close()
                    break
                except Exception:
                    continue
            M.logout()
            return contacts

        try:
            contacts = await asyncio.get_event_loop().run_in_executor(None, _scan_sent)
            _KNOWN_CONTACTS = contacts
            _save_whitelist(contacts)
            log.info(f"email: whitelist construite — {len(contacts)} contacts")
            return (
                f"✅ *Whitelist construite — {len(contacts)} contacts connus*\n"
                f"Sauvegardée dans `{_WHITELIST_FILE}`\n\n"
                f"Lance `trier_boite` pour trier tes 5500+ emails."
            )
        except Exception as e:
            log.error(f"email.construire_whitelist: {e}")
            return f"❌ Erreur construction whitelist: {e}"

    async def trier_boite(self, ctx: dict | None = None) -> str:
        """
        Tri en masse de l'INBOX :
        - Expéditeur dans la whitelist → reste dans Courriels (INBOX)
        - Expéditeur inconnu → déplacé dans Archives (INBOX.Archives)
        Traite tous les emails, par lots de 200, via UID MOVE.
        """
        global _KNOWN_CONTACTS
        ctx = ctx or {}

        if not _KNOWN_CONTACTS:
            _KNOWN_CONTACTS = _load_whitelist()
        if not _KNOWN_CONTACTS:
            return "⚠️ Whitelist vide. Lance d'abord `construire_whitelist`."

        def _run_sort():
            M = _connect()
            M.select("INBOX")
            typ, data = M.uid("search", None, "ALL")
            all_uids = data[0].split() if data[0] else []

            moved = 0
            kept  = 0
            errors = 0
            to_move = []

            for i in range(0, len(all_uids), 200):
                batch = all_uids[i:i+200]
                uid_list = b",".join(batch).decode()
                try:
                    typ2, msgs = M.uid("fetch", uid_list, "(RFC822.HEADER)")
                    j = 0
                    for item in msgs:
                        if not isinstance(item, tuple):
                            continue
                        uid_b = batch[j] if j < len(batch) else None
                        j += 1
                        if uid_b is None:
                            continue
                        uid_s = uid_b.decode()
                        try:
                            msg = email.message_from_bytes(item[1])
                            from_raw = _decode(msg.get("From", ""))
                            m = re.search(r"<([^>]+)>", from_raw)
                            sender = m.group(1).lower() if m else from_raw.strip().lower()

                            if sender in _KNOWN_CONTACTS:
                                kept += 1
                            else:
                                to_move.append(uid_s)
                                moved += 1
                        except Exception:
                            errors += 1
                except Exception:
                    errors += 1
                    continue

            # Déplacer par lots via UID MOVE (atomique)
            for i in range(0, len(to_move), 500):
                batch_move = to_move[i:i+500]
                uid_set = ",".join(batch_move)
                try:
                    M.uid("MOVE", uid_set, _ARCHIVES_FOLDER)
                except Exception as e:
                    log.error(f"email.trier_boite MOVE error: {e}")

            M.close()
            M.logout()
            return moved, kept, errors

        try:
            moved, kept, errors = await asyncio.get_event_loop().run_in_executor(None, _run_sort)
            return (
                f"✅ *Tri terminé*\n\n"
                f"📥 Gardés dans Courriels : *{kept}* (contacts whitelist)\n"
                f"📁 Déplacés vers Archives : *{moved}*\n"
                f"⚠️ Erreurs ignorées : {errors}\n\n"
                f"_Courriels ne contient que des gens avec qui tu as déjà échangé._"
            )
        except Exception as e:
            log.error(f"email.trier_boite: {e}")
            return f"❌ Erreur tri boite: {e}"

    # ── FILTRES — gestion ─────────────────────────────────────────────────────

    async def mes_filtres(self, ctx: dict | None = None) -> str:
        if not _FILTERS:
            return (
                "📋 *Aucun filtre actif.*\n\n"
                "Exemples de filtres à créer :\n"
                "• _'crée un filtre : si expéditeur contient newsletter → marquer lu et archiver'_\n"
                "• _'crée un filtre : si sujet contient Facture → déplacer vers INBOX.Comptabilité'_\n"
                "• _'crée un filtre : si expéditeur est Tesla → priorité haute'_"
            )
        lines = [f"📋 *{len(_FILTERS)} filtre(s) actif(s)*\n"]
        for i, f in enumerate(_FILTERS, 1):
            conds = " ET ".join(
                f"`{c['field']}` {c['operator']} `{c['value']}`"
                for c in f.get("conditions", [])
            )
            acts = " + ".join(
                f"{a['type']} → `{a.get('target', '')}`"
                for a in f.get("actions", [])
            )
            lines.append(f"*{i}. {f['name']}*\n   Si {conds}\n   Alors {acts}\n")
        lines.append("_`appliquer_filtres` pour traiter la boîte maintenant._")
        return "\n".join(lines)

    async def creer_filtre(self, ctx: dict | None = None) -> str:
        """
        Crée un filtre de tri. Le contexte peut contenir :
          - name : nom du filtre
          - conditions : liste [{field, operator, value}]
          - actions : liste [{type, target}]
        Ou bien 'description' en langage naturel → GPT parse les règles.
        """
        ctx = ctx or {}
        description = ctx.get("description", ctx.get("filtre", ""))

        if description:
            # GPT parse la description en JSON structuré
            def _parse():
                import openai
                client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": (
                            "Tu convertis une description de filtre email en JSON structuré. "
                            "Champs disponibles : from_email, from_domain, subject, body, has_attachment. "
                            "Opérateurs : contains, equals, starts_with, not_contains. "
                            "Actions : move_to (target=nom_dossier), mark_read, delete, label (target=étiquette), "
                            "add_score (target=+20 ou -30). "
                            "Retourne UNIQUEMENT le JSON, sans texte autour. Format :\n"
                            '{"name": "...", "conditions": [{"field": "...", "operator": "...", "value": "..."}], '
                            '"actions": [{"type": "...", "target": "..."}]}'
                        )},
                        {"role": "user", "content": f"Crée ce filtre : {description}"},
                    ],
                    max_tokens=400,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content)

            try:
                rule = await asyncio.get_event_loop().run_in_executor(None, _parse)
            except Exception as e:
                return f"❌ Erreur parsing filtre: {e}"
        else:
            rule = {
                "name":       ctx.get("name", "Filtre sans nom"),
                "conditions": ctx.get("conditions", []),
                "actions":    ctx.get("actions", []),
            }

        if not rule.get("conditions") or not rule.get("actions"):
            return "❌ Filtre invalide — conditions ou actions manquantes."

        import uuid
        rule["id"] = str(uuid.uuid4())[:8]
        _FILTERS.append(rule)
        _save_filters(_FILTERS)  # Persister immédiatement

        # Si une action move_to est demandée, créer le dossier IMAP si nécessaire
        for action in rule.get("actions", []):
            if action.get("type") == "move_to" and action.get("target"):
                folder = f"INBOX.{action['target']}"
                try:
                    def _create_folder(f=folder):
                        M = _connect()
                        M.create(f)
                        M.logout()
                    await asyncio.get_event_loop().run_in_executor(None, _create_folder)
                except Exception:
                    pass  # Le dossier existe peut-être déjà

        conds = " ET ".join(
            f"`{c['field']}` {c['operator']} `{c['value']}`"
            for c in rule.get("conditions", [])
        )
        acts = " + ".join(
            f"{a['type']}" + (f" → `{a['target']}`" if a.get("target") else "")
            for a in rule.get("actions", [])
        )
        return (
            f"✅ *Filtre créé* : {rule['name']}\n"
            f"   Si {conds}\n"
            f"   Alors {acts}\n\n"
            f"_Tape 'appliquer les filtres' pour traiter la boîte maintenant._"
        )

    async def appliquer_filtres(self, ctx: dict | None = None) -> str:
        """Applique tous les filtres actifs sur les emails récents."""
        if not _FILTERS:
            return "❌ Aucun filtre à appliquer. Crée d'abord des filtres."

        ctx   = ctx or {}
        limit = int(ctx.get("limit", 200))

        def _run():
            M = _connect()
            M.select("INBOX")
            typ, data = M.search(None, "ALL")
            all_ids = data[0].split() if data[0] else []
            target  = all_ids[-limit:]

            applied_count = 0
            log_actions   = []

            for uid in target:
                uid_s = _uid_str(uid)
                try:
                    typ2, msg_data = M.fetch(uid_s, "(RFC822.HEADER)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    from_raw = _decode(msg.get("From", ""))
                    subject  = _decode(msg.get("Subject", ""))
                    from_match = re.search(r"<([^>]+)>", from_raw)
                    from_email = from_match.group(1).lower() if from_match else from_raw.lower()
                    from_domain = from_email.split("@")[-1] if "@" in from_email else ""

                    for rule in _FILTERS:
                        match = True
                        for cond in rule.get("conditions", []):
                            field    = cond.get("field", "")
                            operator = cond.get("operator", "contains")
                            value    = cond.get("value", "").lower()

                            if field == "from_email":
                                target_str = from_email
                            elif field == "from_domain":
                                target_str = from_domain
                            elif field == "subject":
                                target_str = subject.lower()
                            else:
                                target_str = ""

                            if operator == "contains" and value not in target_str:
                                match = False; break
                            elif operator == "equals" and target_str != value:
                                match = False; break
                            elif operator == "starts_with" and not target_str.startswith(value):
                                match = False; break
                            elif operator == "not_contains" and value in target_str:
                                match = False; break

                        if match:
                            for action in rule.get("actions", []):
                                atype  = action.get("type", "")
                                target_folder = action.get("target", "")
                                try:
                                    if atype == "mark_read":
                                        M.store(uid_s, "+FLAGS", "\\Seen")
                                    elif atype == "delete":
                                        M.store(uid_s, "+FLAGS", "\\Deleted")
                                    elif atype == "move_to" and target_folder:
                                        dest = f"INBOX.{target_folder}"
                                        M.uid("MOVE", uid_s, dest)
                                except Exception:
                                    pass
                            applied_count += 1
                            log_actions.append(f"[{uid_s}] {subject[:40]} → {rule['name']}")
                except Exception:
                    continue

            M.expunge()
            M.logout()
            return applied_count, log_actions

        try:
            count, logs = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            return f"❌ Erreur application filtres: {e}"

        if count == 0:
            return f"✅ Filtres appliqués sur {limit} emails — *aucun email ne correspondait* aux règles."

        log_str = "\n".join(f"  • {l}" for l in logs[:15])
        more = f"\n  _...et {len(logs)-15} autres_" if len(logs) > 15 else ""
        return (
            f"✅ *{count} emails traités* par {len(_FILTERS)} filtre(s)\n\n"
            f"{log_str}{more}"
        )


    # ── AUTO TRIER — triage horaire automatique ───────────────────────────────

    # Expéditeurs critiques qui ne doivent JAMAIS atterrir en Archives
    _CRITICAL_DOMAINS = (
        "cal.com", "calendly.com", "acuityscheduling.com",
        "quickbooks.intuit.com", "stripe.com", "paypal.com",
        "docusign.com", "hellosign.com", "pandadoc.com",
    )

    async def auto_trier(self, ctx: dict | None = None) -> str:
        """
        Triage automatique horaire (multi-comptes WHC + Hostinger) :
        - Scanne les emails des 48 dernières heures dans INBOX (SINCE, pas UNSEEN)
          → corrige le bug où Apple Mail marque les emails comme lus avant l'agent
        - UIDs déjà traités ignorés (fichier JSON, TTL 7 jours)
        - Domaines critiques (Cal.com, paiements…) → toujours garder + notifier
        - Whitelist contact → garder + notifier
        - Email direct en français (pas bulk) → garder + notifier
        - Tout le reste → MOVE vers INBOX.Archives
        """
        global _KNOWN_CONTACTS
        if not _KNOWN_CONTACTS:
            _KNOWN_CONTACTS = _load_whitelist()

        def _run():
            processed_uids = _load_processed_uids()
            all_kept: list[tuple] = []   # (uid, from, subject, account_label)
            total_archived = 0
            today_str = datetime.now().strftime("%Y-%m-%d")
            since_date = (datetime.now() - timedelta(hours=48)).strftime("%d-%b-%Y")

            for host, port, user, password, label in _ALL_ACCOUNTS:
                M = _connect_account(host, port, user, password)
                if M is None:
                    continue
                try:
                    M.select("INBOX")
                    # SINCE au lieu de UNSEEN — Apple Mail marque déjà comme lu
                    typ, data = M.uid("search", None, f"SINCE {since_date}")
                    all_uids = data[0].split() if data[0] else []

                    acct_processed = processed_uids.get(label, {})
                    kept: list[tuple] = []
                    to_archive: list[str] = []

                    for uid_b in all_uids:
                        uid_s = uid_b.decode() if isinstance(uid_b, bytes) else str(uid_b)
                        if uid_s in acct_processed:
                            continue   # déjà traité

                        try:
                            typ2, msg_data = M.uid("fetch", uid_s, "(RFC822)")
                            if not msg_data or not isinstance(msg_data[0], tuple):
                                continue
                            msg = email.message_from_bytes(msg_data[0][1])

                            from_raw = _decode(msg.get("From", ""))
                            subject  = _decode(msg.get("Subject", "(sans sujet)"))
                            m_match  = re.search(r"<([^>]+)>", from_raw)
                            sender   = m_match.group(1).lower() if m_match else from_raw.strip().lower()

                            acct_processed[uid_s] = today_str   # marquer traité

                            if any(d in sender for d in self._CRITICAL_DOMAINS):
                                kept.append((uid_s, from_raw, subject, label))
                            elif sender in _KNOWN_CONTACTS:
                                kept.append((uid_s, from_raw, subject, label))
                            elif _is_direct_french(msg):
                                kept.append((uid_s, from_raw, subject, label))
                            else:
                                to_archive.append(uid_s)
                        except Exception as e:
                            log.warning(f"auto_trier fetch {uid_s} ({label}): {e}")
                            continue

                    processed_uids[label] = acct_processed
                    _save_processed_uids(processed_uids)

                    if to_archive:
                        for i in range(0, len(to_archive), 500):
                            batch = to_archive[i:i + 500]
                            try:
                                M.uid("MOVE", ",".join(batch), _ARCHIVES_FOLDER)
                            except Exception as e:
                                log.warning(f"auto_trier MOVE ({label}): {e}")

                    all_kept.extend(kept)
                    total_archived += len(to_archive)
                except Exception as e:
                    log.warning(f"auto_trier {label}: {e}")
                finally:
                    try:
                        M.logout()
                    except Exception:
                        pass

            return all_kept, total_archived

        try:
            kept, archived_count = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            log.error(f"email.auto_trier: {e}")
            return f"❌ auto_trier erreur: {e}"

        total = len(kept) + archived_count
        if total == 0:
            log.info("email.auto_trier: aucun nouveau message")
            return "✅ auto_trier: aucun nouveau message"

        if kept:
            from core.telegram_notifier import notify
            lines = [f"📬 *{len(kept)} nouveau(x) email(s) important(s)*\n"]
            for uid_s, from_raw, subject, acct in kept[:5]:
                lines.append(f"• [{acct}] {from_raw[:45]}\n  _{subject[:60]}_")
            if len(kept) > 5:
                lines.append(f"_...et {len(kept) - 5} autres_")
            if archived_count:
                lines.append(f"\n📁 {archived_count} déplacé(s) → Archives")
            await notify("\n".join(lines))

        log.info(f"email.auto_trier: {len(kept)} gardés, {archived_count} archivés")
        return f"auto_trier: {len(kept)} importants, {archived_count} → Archives"


# ── Instance globale ──────────────────────────────────────────────────────────
agent = EmailAgent()
