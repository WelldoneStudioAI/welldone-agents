#!/usr/bin/env python3
"""
Welldone Studio — Agent Telegram COMPLET v2 (Railway Edition)
Cerveau : Claude (Anthropic)
Capacités : Gmail, Calendar, Zoho Books, Notion, Contacts Google, GA4, SEO, Veille
"""

import os, sys, json, asyncio, datetime, base64, time, requests, logging, subprocess, tempfile
from email.message import EmailMessage
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import anthropic

# ── Config depuis variables d'environnement ────────────────────────────────────
BOT_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
ALLOWED_ID        = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "8434904512"))
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN", "")
NOTION_TASK_DB    = os.environ.get("NOTION_TASK_DB", "bd4cff932b7842b19f7cb748e1abda48")
ZOHO_CLIENT_ID    = os.environ.get("ZOHO_CLIENT_ID", "1000.F14C6MSUQPH52S4P47AHTAVQET3Z3X")
ZOHO_CLIENT_SECRET= os.environ.get("ZOHO_CLIENT_SECRET", "ccde844c1470947175d86f3c4c50dab0e22e2a95ec")
ZOHO_ORG_ID       = os.environ.get("ZOHO_ORG_ID", "110002477093")
ZOHO_BASE_URL     = "https://www.zohoapis.ca/books/v3"

# ── Google Token — reconstruit depuis env var base64 ──────────────────────────
GOOGLE_TOKEN = os.path.join(tempfile.gettempdir(), "google_token.json")
ZOHO_TOKEN_FILE = os.path.join(tempfile.gettempdir(), "zoho_token.json")

def init_tokens():
    """Reconstruit les fichiers de token depuis les variables d'environnement."""
    g_b64 = os.environ.get("GOOGLE_TOKEN_JSON_B64", "")
    if g_b64:
        with open(GOOGLE_TOKEN, "w") as f:
            f.write(base64.b64decode(g_b64).decode())
        log.info("✅ Token Google reconstruit depuis env var")
    else:
        log.warning("⚠️ GOOGLE_TOKEN_JSON_B64 non défini")

    z_b64 = os.environ.get("ZOHO_TOKEN_JSON_B64", "")
    if z_b64:
        with open(ZOHO_TOKEN_FILE, "w") as f:
            f.write(base64.b64decode(z_b64).decode())
        log.info("✅ Token Zoho reconstruit depuis env var")

SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/contacts.readonly',
    'https://www.googleapis.com/auth/contacts.other.readonly'
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(message)s")
log = logging.getLogger(__name__)

# ── État en mémoire ───────────────────────────────────────────────────────────
user_conversations = {}
pending_emails   = {}
pending_invoices = {}
pending_tasks    = {}
pending_events   = {}

# ── Google API ────────────────────────────────────────────────────────────────
def get_google_service(api_name, api_version):
    if os.path.exists(GOOGLE_TOKEN):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN, SCOPES)
        return build(api_name, api_version, credentials=creds)
    return None

# ── Gmail ─────────────────────────────────────────────────────────────────────
async def lire_emails(update, context):
    try:
        service = get_google_service('gmail', 'v1')
        if not service:
            await update.message.reply_text("❌ Token Google introuvable.")
            return
        results = service.users().messages().list(userId='me', labelIds=['UNREAD'], maxResults=3).execute()
        messages = results.get('messages', [])
        if not messages:
            await update.message.reply_text("📭 Aucun nouvel email non lu!")
            return
        reponse = "📧 *Derniers emails non lus :*\n\n"
        for msg in messages:
            data = service.users().messages().get(userId='me', id=msg['id'], format='metadata',
                                                   metadataHeaders=['Subject', 'From']).execute()
            headers = data.get('payload', {}).get('headers', [])
            sujet = next((h['value'] for h in headers if h['name'] == 'Subject'), "Sans sujet")
            expediteur = next((h['value'] for h in headers if h['name'] == 'From'), "Inconnu")
            reponse += f"🔸 De : {expediteur}\n🔹 Sujet : {sujet}\n\n"
        await update.message.reply_text(reponse, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur Gmail : {e}")

async def envoyer_email(update, destinataire, sujet, corps, type_signature="client"):
    try:
        service = get_google_service('gmail', 'v1')
        if not service:
            await update.effective_message.reply_text("❌ Token Google introuvable.")
            return False
        SIG_CLIENT = "\n\nCordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"
        SIG_FACT   = "\n\nCordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313"
        if type_signature == "facturation":
            signature = SIG_FACT
            from_addr = 'billing@awelldone.studio'
        else:
            signature = SIG_CLIENT
            from_addr = 'jptanguay@awelldone.studio'
        msg = EmailMessage()
        msg.set_content(corps + signature)
        msg['To'] = destinataire
        msg['From'] = from_addr
        msg['Subject'] = sujet
        msg['Bcc'] = 'ia@awelldone.studio'
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={'raw': raw}).execute()
        return True
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Erreur envoi email : {e}")
        return False

async def chercher_contact(nom):
    """
    Protocole de résolution de contact :
    1. Google Contacts (contacts + otherContacts)
    2. Fallback Gmail (expéditeurs récents)
    """
    resultats = []
    try:
        service = get_google_service('people', 'v1')
        if service:
            r = service.people().searchContacts(query=nom, readMask='names,emailAddresses').execute()
            for c in r.get('results', []):
                p = c.get('person', {})
                name = p.get('names', [{}])[0].get('displayName', '')
                for e in p.get('emailAddresses', []):
                    resultats.append({"name": name, "email": e.get('value'), "source": "Google Contacts"})
            try:
                r2 = service.otherContacts().search(query=nom, readMask='names,emailAddresses').execute()
                for c in r2.get('results', []):
                    p = c.get('person', {})
                    name = p.get('names', [{}])[0].get('displayName', '')
                    for e in p.get('emailAddresses', []):
                        email = e.get('value')
                        if not any(x['email'] == email for x in resultats):
                            resultats.append({"name": name, "email": email, "source": "Google Contacts (autres)"})
            except: pass
    except Exception as e:
        log.error(f"Erreur contacts: {e}")

    if not resultats:
        try:
            gmail = get_google_service('gmail', 'v1')
            if gmail:
                msgs = gmail.users().messages().list(userId='me', q=f'from:{nom} OR to:{nom}', maxResults=10).execute()
                emails_vus = set()
                for msg in msgs.get('messages', []):
                    data = gmail.users().messages().get(userId='me', id=msg['id'],
                                                        format='metadata', metadataHeaders=['From','To']).execute()
                    headers = data.get('payload', {}).get('headers', [])
                    for h in headers:
                        if h['name'] in ('From', 'To') and nom.lower() in h['value'].lower():
                            import re
                            match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', h['value'])
                            if match:
                                email = match.group(0)
                                display = h['value'].split('<')[0].strip().strip('"') or email
                                if email not in emails_vus:
                                    emails_vus.add(email)
                                    resultats.append({"name": display, "email": email, "source": "Gmail"})
        except Exception as e:
            log.error(f"Erreur Gmail search: {e}")

    if not resultats:
        return {"status": "not_found", "results": []}
    elif len(resultats) == 1:
        return {"status": "found", "results": resultats}
    else:
        seen = {}
        for r in resultats:
            if r['email'] not in seen:
                seen[r['email']] = r
        unique = list(seen.values())
        return {"status": "multiple" if len(unique) > 1 else "found", "results": unique}

# ── Google Calendar ────────────────────────────────────────────────────────────
def creer_evenement_calendar(titre, date_str, heure_str=None, description=None):
    service = get_google_service('calendar', 'v3')
    if not service: return False
    try:
        event = {"summary": titre, "description": description or ""}
        if heure_str:
            dt_start = f"{date_str}T{heure_str}:00"
            dt_end = (datetime.datetime.strptime(f"{date_str} {heure_str}", "%Y-%m-%d %H:%M") +
                      datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:00")
            event["start"] = {"dateTime": dt_start, "timeZone": "America/Toronto"}
            event["end"]   = {"dateTime": dt_end, "timeZone": "America/Toronto"}
        else:
            end_date = (datetime.datetime.strptime(date_str, "%Y-%m-%d") +
                        datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            event["start"] = {"date": date_str}
            event["end"]   = {"date": end_date}
        service.events().insert(calendarId='primary', body=event).execute()
        return True
    except: return False

# ── Notion ─────────────────────────────────────────────────────────────────────
def creer_tache_notion(titre, date_echeance=None, priorite="Moyenne", notes=None):
    if not NOTION_TOKEN or not NOTION_TASK_DB:
        return None, "Notion non configuré"
    properties = {
        "Nom": {"title": [{"text": {"content": titre}}]},
        "Statut": {"select": {"name": "À faire"}},
        "Priorité": {"select": {"name": priorite}},
        "Créé par IA": {"checkbox": True},
    }
    if date_echeance:
        properties["Date"] = {"date": {"start": date_echeance}}
    if notes:
        properties["Notes"] = {"rich_text": [{"text": {"content": notes}}]}
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    resp = requests.post("https://api.notion.com/v1/pages",
                         headers=headers, json={"parent": {"database_id": NOTION_TASK_DB}, "properties": properties})
    data = resp.json()
    return (data["id"], data.get("url", "")) if "id" in data else (None, data.get("message", "Erreur"))

# ── Zoho Books ─────────────────────────────────────────────────────────────────
def get_zoho_token():
    if not os.path.exists(ZOHO_TOKEN_FILE): return None
    with open(ZOHO_TOKEN_FILE) as f: data = json.load(f)
    if time.time() > data.get("expires_at", 0) - 300:
        resp = requests.post("https://accounts.zohocloud.ca/oauth/v2/token", data={
            "refresh_token": data["refresh_token"], "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET, "grant_type": "refresh_token"
        }).json()
        if "access_token" not in resp: return None
        data["access_token"] = resp["access_token"]
        data["expires_at"] = time.time() + resp.get("expires_in", 3600)
        with open(ZOHO_TOKEN_FILE, "w") as f: json.dump(data, f)
    return data["access_token"]

def zoho_headers():
    return {"Authorization": f"Zoho-oauthtoken {get_zoho_token()}", "Content-Type": "application/json"}

def chercher_facture_zoho(client_name=None, invoice_number=None):
    params = {"organization_id": ZOHO_ORG_ID, "per_page": 5, "sort_column": "date", "sort_order": "D"}
    if invoice_number: params["invoice_number"] = invoice_number
    elif client_name: params["customer_name_contains"] = client_name
    return requests.get(f"{ZOHO_BASE_URL}/invoices", headers=zoho_headers(), params=params).json().get("invoices", [])

def envoyer_facture_zoho_api(invoice_id):
    resp = requests.post(f"{ZOHO_BASE_URL}/invoices/{invoice_id}/email",
                         headers=zoho_headers(), params={"organization_id": ZOHO_ORG_ID}, json={}).json()
    return resp.get("code") == 0

# ── Claude (cerveau) ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""Tu es l'assistant personnel de Jean-Philippe Roy (JP), fondateur de Welldone | Studio à Montréal.

CONTACTS CONNUS (utilise directement ces emails, pas besoin de chercher) :
- JP / Jean-Philippe / Jean-Philippe Roy / Jean-Philippe Roy Tanguay → jptanguay@awelldone.com
- Facturation Welldone → billing@awelldone.studio
- IA interne → ia@awelldone.studio

PROTOCOLE DE RÉSOLUTION DE CONTACT (pour tout autre contact) :
1. Ne jamais inventer ni deviner une adresse email
2. Si le nom est ambigu ou inconnu → utiliser contact_name (le système cherchera dans Google Contacts puis Gmail)
3. Si plusieurs adresses sont trouvées → le système présentera des choix à JP automatiquement
4. Si aucun résultat → informer JP clairement et lui demander l'adresse directement
5. Toujours indiquer la source : Google Contacts ou Gmail

⛔ RÈGLE ABSOLUE — ENVOI DE COURRIEL :
Aucun courriel n'est jamais envoyé sans que JP ait vu la prévisualisation complète et cliqué sur "Envoyer".
Le système affiche TOUJOURS : À, Sujet, Corps complet, Signature — avec boutons Envoyer / Modifier / Annuler.
Ne jamais sauter cette étape, même si JP dit "envoie directement".

Tu parles en français québécois, de façon naturelle, chaleureuse et professionnelle.
Aujourd'hui : {datetime.datetime.now().strftime("%A %d %B %Y %H:%M")}.

TES CAPACITÉS (réponds en JSON strict) :

INTENTS DISPONIBLES :
- read_emails : Lire les emails non lus
- send_email : Rédiger et envoyer un email
- add_event : Créer un événement Google Calendar (besoin d'une heure précise)
- create_task : Créer une tâche dans Notion + Calendar
- send_invoice : Envoyer une facture depuis Zoho Books
- run_rapport : Déclencher le rapport GA4 + Search Console (via GitHub Actions)
- run_veille : Lancer la veille de contenu maintenant (via GitHub Actions)
- morning_brief : Résumé d'arrivée au bureau
- chat : Conversation, rédaction, aide, contenu, brainstorm

RÈGLES EMAIL :
- signature_type "client" → tutoiement, ton chaleureux et professionnel
- signature_type "facturation" → vouvoiement, ton formel, sobre
- NE JAMAIS inclure de signature dans email_body (gérée automatiquement)
- Salutation + phrase de contexte + corps + formule de politesse finale
- Style : humain, poli, jamais généré-par-IA

RÈGLE TÂCHE vs ÉVÉNEMENT :
- add_event = rendez-vous avec HEURE PRÉCISE mentionnée
- create_task = action à faire, rappel, suivi (même avec une date sans heure)
- Si ambigu → intent "chat" + poser une question courte

FORMAT JSON OBLIGATOIRE :
{{
  "intent": "...",
  "reply": "réponse courte pour l'utilisateur",

  // Pour send_email :
  "email_address": null ou "email@exemple.com",
  "contact_name": null ou "Prénom Nom",
  "email_subject": "Sujet",
  "email_body": "Corps complet sans signature",
  "signature_type": "client" ou "facturation",

  // Pour add_event :
  "summary": "Titre",
  "date": "YYYY-MM-DD",
  "time": "HH:MM",

  // Pour create_task :
  "titre": "Titre de la tâche",
  "date": null ou "YYYY-MM-DD",
  "heure": null ou "HH:MM",
  "priorite": "Haute/Moyenne/Basse",
  "notes": null ou "notes",

  // Pour send_invoice :
  "client_name": null ou "Nom",
  "invoice_number": null ou "INV-XXX"
}}
"""

async def ask_claude(user_id, message):
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": message})
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=user_conversations[user_id]
        )
        reply_text = resp.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": reply_text})
        if "```" in reply_text:
            reply_text = reply_text.split("```")[1].replace("json", "").strip()
        return json.loads(reply_text)
    except json.JSONDecodeError:
        return {"intent": "chat", "reply": resp.content[0].text}
    except Exception as e:
        return {"intent": "chat", "reply": f"Erreur: {e}"}

# ── Traitement des intents ─────────────────────────────────────────────────────
async def process_intent(update, intent_data):
    intent = intent_data.get("intent")
    reply  = intent_data.get("reply", "")
    user_id = update.effective_user.id

    if reply:
        await update.message.reply_text(reply)

    if intent == "read_emails":
        await lire_emails(update, None)

    elif intent == "send_email":
        destinataire = intent_data.get("email_address")
        nom_contact  = intent_data.get("contact_name")
        sujet = intent_data.get("email_subject", "Message de Welldone Studio")
        corps = intent_data.get("email_body", "")
        type_sig = intent_data.get("signature_type", "client")

        if not destinataire and nom_contact:
            await update.message.reply_text(f"🔍 Recherche de '{nom_contact}'...")
            resultat = await chercher_contact(nom_contact)
            status  = resultat["status"]
            results = resultat["results"]

            if status == "not_found":
                await update.message.reply_text(
                    f"❌ Aucun résultat trouvé pour '{nom_contact}' ni dans Google Contacts ni dans Gmail.\n"
                    f"Précise l'adresse email directement.")
                return
            elif status == "multiple":
                pending_emails[user_id] = {
                    "sujet": sujet, "corps": corps, "contact_name": nom_contact,
                    "type_signature": type_sig, "choix_contacts": results
                }
                texte = f"🔎 Plusieurs contacts trouvés pour *{nom_contact}* — lequel veux-tu ?\n\n"
                keyboard = []
                for i, r in enumerate(results[:5]):
                    label = f"{r['name']} — {r['email']} ({r['source']})"
                    texte += f"{i+1}. {label}\n"
                    keyboard.append([InlineKeyboardButton(f"{i+1}. {r['email']}", callback_data=f"contact_pick_{i}")])
                keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="email_cancel")])
                await update.message.reply_text(texte, parse_mode="Markdown",
                                                reply_markup=InlineKeyboardMarkup(keyboard))
                return
            else:
                r = results[0]
                destinataire = r["email"]
                nom_contact  = r["name"]

        if not destinataire or not corps:
            await update.message.reply_text("🤔 Il me manque l'adresse ou le contenu du message.")
            return

        # ── PRÉVISUALISATION OBLIGATOIRE ──
        pending_emails[user_id] = {
            "destinataire": destinataire, "sujet": sujet, "corps": corps,
            "contact_name": nom_contact, "type_signature": type_sig
        }
        SIG_PREVIEW = "Cordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313" \
                      if type_sig == "facturation" else \
                      "Cordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"
        nom_aff = nom_contact if nom_contact else destinataire
        preview = (f"📝 <b>Prévisualisation</b>\n\n"
                   f"<b>À :</b> {nom_aff} &lt;{destinataire}&gt;\n"
                   f"<b>Sujet :</b> {sujet}\n\n"
                   f"<b>Message :</b>\n{corps}\n\n"
                   f"<code>{SIG_PREVIEW}</code>\n\n"
                   f"⚠️ <i>Aucun envoi sans ta confirmation.</i>")
        keyboard = [[InlineKeyboardButton("✅ Envoyer", callback_data="email_ok"),
                     InlineKeyboardButton("✏️ Modifier", callback_data="email_edit")],
                    [InlineKeyboardButton("❌ Annuler", callback_data="email_cancel")]]
        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))

    elif intent == "add_event":
        summary  = intent_data.get("summary", "Rendez-vous")
        date_str = intent_data.get("date")
        time_str = intent_data.get("time")
        if not date_str or not time_str:
            await update.message.reply_text("🤔 J'ai besoin d'une date et d'une heure précise.")
            return
        pending_events[user_id] = {"summary": summary, "date": date_str, "time": time_str}
        preview = (f"📅 <b>Nouveau rendez-vous</b>\n\n"
                   f"<b>Titre :</b> {summary}\n"
                   f"<b>Date :</b> {date_str} à {time_str}\n"
                   f"<b>Durée :</b> 1 heure\n\n"
                   f"<i>Ajouter à Google Calendar ?</i>")
        keyboard = [[InlineKeyboardButton("✅ Confirmer", callback_data="event_ok"),
                     InlineKeyboardButton("❌ Annuler", callback_data="event_cancel")]]
        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))

    elif intent == "create_task":
        titre    = intent_data.get("titre")
        date_str = intent_data.get("date")
        heure    = intent_data.get("heure")
        priorite = intent_data.get("priorite", "Moyenne")
        notes    = intent_data.get("notes")
        if not titre:
            await update.message.reply_text("🤔 J'ai besoin d'un titre pour la tâche.")
            return
        pending_tasks[user_id] = {"titre": titre, "date": date_str, "heure": heure,
                                   "priorite": priorite, "notes": notes}
        preview = (f"📋 <b>Nouvelle tâche</b>\n\n"
                   f"<b>Titre :</b> {titre}\n" +
                   (f"<b>Date :</b> {date_str}" + (f" à {heure}" if heure else "") + "\n" if date_str else "") +
                   f"<b>Priorité :</b> {priorite}\n" +
                   (f"<b>Notes :</b> {notes}\n" if notes else "") +
                   "\n<i>Créer dans Notion + Google Calendar ?</i>")
        keyboard = [[InlineKeyboardButton("✅ Créer", callback_data="task_ok"),
                     InlineKeyboardButton("❌ Annuler", callback_data="task_cancel")]]
        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))

    elif intent == "send_invoice":
        client_name    = intent_data.get("client_name")
        invoice_number = intent_data.get("invoice_number")
        if not get_zoho_token():
            await update.message.reply_text("❌ Zoho Books non connecté.")
            return
        await update.message.reply_text("🔍 Recherche dans Zoho Books...")
        factures = chercher_facture_zoho(client_name=client_name, invoice_number=invoice_number)
        if not factures:
            await update.message.reply_text(f"❌ Aucune facture trouvée pour '{client_name or invoice_number}'.")
            return
        f = factures[0]
        pending_invoices[user_id] = {"invoice_id": f["invoice_id"],
                                      "invoice_number": f["invoice_number"], "client": f["customer_name"]}
        preview = (f"🧾 <b>Facture Zoho Books</b>\n\n"
                   f"<b>Numéro :</b> {f['invoice_number']}\n"
                   f"<b>Client :</b> {f['customer_name']}\n"
                   f"<b>Montant :</b> {f['total']} {f.get('currency_code','CAD')}\n"
                   f"<b>Date :</b> {f['date']}\n"
                   f"<b>Statut :</b> {f['status']}\n\n"
                   f"<i>Envoyer cette facture par email au client ?</i>")
        keyboard = [[InlineKeyboardButton("✅ Envoyer", callback_data="invoice_ok"),
                     InlineKeyboardButton("❌ Annuler", callback_data="invoice_cancel")]]
        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))

    elif intent == "run_rapport":
        await update.message.reply_text("📊 Rapport en cours — tu recevras ton email dans 2-3 min...")
        try:
            gh_token = os.environ.get("GITHUB_PAT", "")
            if gh_token:
                requests.post(
                    "https://api.github.com/repos/WelldoneStudioAI/welldone-agents/actions/workflows/rapport.yml/dispatches",
                    headers={"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"},
                    json={"ref": "main"}
                )
                await update.message.reply_text("✅ Rapport déclenché! Email dans 2-3 min.")
            else:
                await update.message.reply_text("⚠️ GITHUB_PAT non configuré — rapport non disponible depuis Railway.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erreur déclenchement : {e}")

    elif intent == "run_veille":
        await update.message.reply_text("🔍 Veille en cours — tu recevras ton email dans 3-5 min...")
        try:
            gh_token = os.environ.get("GITHUB_PAT", "")
            if gh_token:
                requests.post(
                    "https://api.github.com/repos/WelldoneStudioAI/welldone-agents/actions/workflows/veille.yml/dispatches",
                    headers={"Authorization": f"token {gh_token}", "Accept": "application/vnd.github.v3+json"},
                    json={"ref": "main"}
                )
                await update.message.reply_text("✅ Veille déclenchée! Check ton email dans 5 min.")
            else:
                await update.message.reply_text("⚠️ GITHUB_PAT non configuré.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erreur : {e}")

    elif intent == "morning_brief":
        now = datetime.datetime.now()
        brief = (f"☀️ *Brief du {now.strftime('%A %d %B %Y')}*\n\n"
                 f"📊 Dis 'rapport' pour les stats GA4\n"
                 f"🔍 Dis 'veille' pour lancer la veille\n"
                 f"📧 Dis 'lis mes emails' pour voir les nouveaux messages\n"
                 f"📋 Dis 'crée une tâche...' pour ajouter à Notion")
        await update.message.reply_text(brief, parse_mode="Markdown")

# ── Callbacks boutons ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data.startswith("contact_pick_"):
        idx = int(data.replace("contact_pick_", ""))
        if user_id in pending_emails:
            e = pending_emails[user_id]
            choix = e.get("choix_contacts", [])
            if idx < len(choix):
                r = choix[idx]
                destinataire = r["email"]
                pending_emails[user_id]["destinataire"] = destinataire
                pending_emails[user_id].pop("choix_contacts", None)
                SIG_PREVIEW = "Cordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313" \
                              if e.get("type_signature") == "facturation" else \
                              "Cordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"
                preview = (f"📝 <b>Prévisualisation — {r['name']}</b>\n\n"
                           f"<b>À :</b> {destinataire}\n"
                           f"<b>Source :</b> {r['source']}\n"
                           f"<b>Sujet :</b> {e['sujet']}\n"
                           f"<b>Message :</b>\n{e['corps']}\n\n"
                           f"<code>{SIG_PREVIEW}</code>\n\n"
                           f"<i>Envoyer ce message ?</i>")
                keyboard = [[InlineKeyboardButton("✅ Envoyer", callback_data="email_ok"),
                             InlineKeyboardButton("✏️ Modifier", callback_data="email_edit")],
                            [InlineKeyboardButton("❌ Annuler", callback_data="email_cancel")]]
                await query.edit_message_text(preview, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "email_ok":
        if user_id in pending_emails:
            e = pending_emails[user_id]
            nom = e.get('contact_name') or e['destinataire']
            ok = await envoyer_email(update, e["destinataire"], e["sujet"], e["corps"], e.get("type_signature", "client"))
            await query.edit_message_text(f"✈️ ✅ Email envoyé à {nom}!" if ok else "❌ Échec de l'envoi.")
            del pending_emails[user_id]
        else:
            await query.edit_message_text("⚠️ Email plus en attente.")
    elif data == "email_edit":
        if user_id in pending_emails: del pending_emails[user_id]
        await query.message.reply_html("✏️ <b>Mode modification</b>\nDis-moi ce que tu veux changer :\n<i>Ex: 'Rends-le plus formel', 'Ajoute que le délai est vendredi'</i>")
    elif data == "email_cancel":
        if user_id in pending_emails: del pending_emails[user_id]
        await query.edit_message_text("❌ Email annulé.")

    elif data == "invoice_ok":
        if user_id in pending_invoices:
            inv = pending_invoices[user_id]
            await query.edit_message_text(f"🔄 Envoi facture #{inv['invoice_number']} à {inv['client']}...")
            ok = envoyer_facture_zoho_api(inv["invoice_id"])
            await query.edit_message_text(f"✅ Facture #{inv['invoice_number']} envoyée à {inv['client']}!" if ok else "❌ Échec Zoho.")
            del pending_invoices[user_id]
        else:
            await query.edit_message_text("⚠️ Facture plus en attente.")
    elif data == "invoice_cancel":
        if user_id in pending_invoices: del pending_invoices[user_id]
        await query.edit_message_text("❌ Envoi facture annulé.")

    elif data == "task_ok":
        if user_id in pending_tasks:
            t = pending_tasks[user_id]
            msgs = []
            task_id, url = creer_tache_notion(t["titre"], t.get("date"), t.get("priorite", "Moyenne"), t.get("notes"))
            msgs.append("✅ Tâche créée dans Notion" if task_id else f"⚠️ Notion : {url}")
            if t.get("date"):
                ok = creer_evenement_calendar(f"📋 {t['titre']}", t["date"], t.get("heure"), t.get("notes"))
                msgs.append("✅ Ajouté à Google Calendar" if ok else "⚠️ Échec Calendar")
            del pending_tasks[user_id]
            await query.edit_message_text("\n".join(msgs))
        else:
            await query.edit_message_text("⚠️ Tâche plus en attente.")
    elif data == "task_cancel":
        if user_id in pending_tasks: del pending_tasks[user_id]
        await query.edit_message_text("❌ Tâche annulée.")

    elif data == "event_ok":
        if user_id in pending_events:
            ev = pending_events[user_id]
            try:
                service = get_google_service('calendar', 'v3')
                start_dt = f"{ev['date']}T{ev['time']}:00-04:00"
                end_obj  = datetime.datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)
                end_dt   = end_obj.strftime("%Y-%m-%dT%H:%M:00-04:00")
                service.events().insert(calendarId='primary', body={
                    'summary': ev['summary'],
                    'start': {'dateTime': start_dt}, 'end': {'dateTime': end_dt}
                }).execute()
                del pending_events[user_id]
                await query.edit_message_text(f"📅 ✅ '{ev['summary']}' ajouté à Google Calendar!")
            except Exception as e:
                await query.edit_message_text(f"⚠️ Erreur Calendar : {e}")
        else:
            await query.edit_message_text("⚠️ Événement plus en attente.")
    elif data == "event_cancel":
        if user_id in pending_events: del pending_events[user_id]
        await query.edit_message_text("❌ Rendez-vous annulé.")

# ── Message principal ──────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_ID: return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    intent_data = await ask_claude(update.effective_user.id, update.message.text)
    await process_intent(update, intent_data)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_ID: return
    user_conversations[update.effective_user.id] = []
    await update.message.reply_html(
        f"Bonjour JP ! Je suis ton agent Welldone, propulsé par Claude. 🧠🚀\n\n"
        "Parle-moi naturellement :\n"
        "🔹 <i>'As-tu des nouveaux emails pour moi ?'</i>\n"
        "🔹 <i>'Envoie un rappel de facture à Playground'</i>\n"
        "🔹 <i>'Ajoute une réunion lundi à 14h'</i>\n"
        "🔹 <i>'Donne-moi mes stats GA4'</i>\n"
        "🔹 <i>'Lance la veille de contenu'</i>\n"
        "🔹 <i>'Rédige un email à Jean pour lui rappeler le paiement'</i>"
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_ID: return
    user_conversations[update.effective_user.id] = []
    await update.message.reply_text("🔄 Conversation remise à zéro!")

async def cmd_emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_ID: return
    await lire_emails(update, context)

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    log.info("🤖 Welldone Agent v2 (Railway) démarré")
    init_tokens()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("emails", cmd_emails))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("✅ En écoute...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
