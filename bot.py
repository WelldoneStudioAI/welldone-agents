import os
import asyncio
import json
import datetime
import base64
import time
import requests
from email.message import EmailMessage
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- IMPORTS POUR GOOGLE API ---
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- IMPORTS POUR OPENAI ---
from openai import AsyncOpenAI
from agent_voyage import handle_voyage_request

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_ORG_ID = os.getenv("ZOHO_ORG_ID")
ZOHO_TOKEN_FILE = "zoho_token.json"
ZOHO_BASE_URL = "https://www.zohoapis.ca/books/v3"

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# On ajoute le scope pour autoriser l'envoi d'emails et la lecture des contacts et des autres contacts (autocompletion)
SCOPES = [
    'https://mail.google.com/', 
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/contacts.readonly',
    'https://www.googleapis.com/auth/contacts.other.readonly'
]

# Dictionnaire pour stocker la mémoire à court terme des utilisateurs
user_conversations = {}
# Emails en attente de validation
pending_emails = {}
# Factures en attente de validation
pending_invoices = {}
# Tâches en attente de validation
pending_tasks = {}
# Événements Calendar en attente de validation
pending_events = {}

def get_google_service(api_name, api_version):
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        return build(api_name, api_version, credentials=creds)
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_conversations[user.id] = [] # Reset memory
    await update.message.reply_text("Salut ! Je suis ton assistant personnel (Notion, Google Docs, Zoho...) et ton Agent de Voyage expert (/voyage). Dis-moi ce que tu veux faire !")
    await update.message.reply_html(
        rf"Bonjour {user.mention_html()} ! Je suis maintenant propulsé par le cerveau d'OpenAI et je suis connecté à Google. 🧠🚀\n\n"
        "Parlez-moi naturellement ! Par exemple :\n"
        "🔹 <i>'As-tu des nouveaux emails pour moi ?'</i>\n"
        "🔹 <i>'Ajoute un rdv chez le dentiste demain vers 14h'</i>\n"
        "🔹 <i>'Envoie un email à Jean pour lui dire bonjour'</i>"
    )

async def lire_emails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        service = get_google_service('gmail', 'v1')
        if not service:
            await update.message.reply_text("❌ Le fichier token.json est introuvable. Avez-vous validé l'accès Google ?")
            return

        results = service.users().messages().list(userId='me', labelIds=['UNREAD'], maxResults=3).execute()
        messages = results.get('messages', [])

        if not messages:
            await update.message.reply_text("📭 Vous n'avez aucun nouvel email non lu !")
            return

        reponse = "📧 Voici vos derniers emails non lus :\n\n"
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['Subject', 'From']).execute()
            headers = msg_data.get('payload', {}).get('headers', [])
            
            sujet = next((h['value'] for h in headers if h['name'] == 'Subject'), "Sans sujet")
            expediteur = next((h['value'] for h in headers if h['name'] == 'From'), "Inconnu")
            
            reponse += f"🔸 De : {expediteur}\n🔹 Sujet : {sujet}\n\n"

        await update.message.reply_text(reponse)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Une erreur s'est produite avec Gmail (Lecture) : {str(e)}")

async def envoyer_email(update: Update, destinataire: str, sujet: str, corps: str, type_signature: str = "client") -> bool:
    try:
        service = get_google_service('gmail', 'v1')
        if not service:
             await update.effective_message.reply_text("❌ Le fichier token.json est introuvable.")
             return False
             
        SIGNATURE_CLIENT = """\n\nCordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"""
        SIGNATURE_FACTURATION = """\n\nCordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313"""
        
        if type_signature == "facturation":
            signature = SIGNATURE_FACTURATION
            adresse_from = 'billing@awelldone.studio'
        else:
            signature = SIGNATURE_CLIENT
            adresse_from = 'jptanguay@awelldone.studio'
            
        corps_complet = corps + signature
        
        message = EmailMessage()
        message.set_content(corps_complet)
        message['To'] = destinataire
        message['From'] = adresse_from
        message['Subject'] = sujet
        message['Bcc'] = 'ia@awelldone.studio'  # Journal interne de tous les envois

        # Encodage requis par l'API Gmail
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}

        service.users().messages().send(userId="me", body=create_message).execute()
        return True
        
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Erreur lors de l'envoi de l'email : {str(e)}")
        return False

async def chercher_contact(prenom_ou_nom: str) -> str:
    """Recherche un email dans les contacts Google de l'utilisateur"""
    try:
        service = get_google_service('people', 'v1')
        if not service:
            return None
            
        # 1. On recherche d'abord dans les contacts (limité à 10 pour simplifier)
        results = service.people().searchContacts(query=prenom_ou_nom, readMask='names,emailAddresses').execute()
        contacts = results.get('results', [])
        
        for contact_result in contacts:
            person = contact_result.get('person', {})
            emails = person.get('emailAddresses', [])
            if emails:
                return emails[0].get('value')
                
        # 2. Si non trouvé, on cherche dans otherContacts (auto-saved from Gmail)
        other_results = service.otherContacts().search(query=prenom_ou_nom, readMask='names,emailAddresses').execute()
        other_contacts = other_results.get('results', [])
        
        for c in other_contacts:
            person = c.get('person', {})
            emails = person.get('emailAddresses', [])
            if emails:
                return emails[0].get('value')
                
        return None
    except Exception as e:
        print(f"Erreur recherche contact: {e}")
        return None

# ==========================================
# FONCTIONS ZOHO BOOKS
# ==========================================

def get_zoho_token():
    """Charge le token Zoho et le rafraîchit si expiré."""
    if not os.path.exists(ZOHO_TOKEN_FILE):
        return None
    with open(ZOHO_TOKEN_FILE) as f:
        data = json.load(f)
    if time.time() > data.get("expires_at", 0) - 300:
        resp = requests.post("https://accounts.zohocloud.ca/oauth/v2/token", data={
            "refresh_token": data["refresh_token"],
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        })
        new = resp.json()
        if "access_token" not in new:
            return None
        data["access_token"] = new["access_token"]
        data["expires_at"] = time.time() + new.get("expires_in", 3600)
        with open(ZOHO_TOKEN_FILE, "w") as f:
            json.dump(data, f)
    return data["access_token"]

def zoho_headers():
    token = get_zoho_token()
    return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}

def chercher_facture_zoho(client_name=None, invoice_number=None):
    """Cherche une facture dans Zoho Books par numéro ou nom de client."""
    params = {"organization_id": ZOHO_ORG_ID, "per_page": 5, "sort_column": "date", "sort_order": "D"}
    if invoice_number:
        params["invoice_number"] = invoice_number
    elif client_name:
        params["customer_name_contains"] = client_name
    resp = requests.get(f"{ZOHO_BASE_URL}/invoices", headers=zoho_headers(), params=params)
    return resp.json().get("invoices", [])

# ======================== NOTION + CALENDAR ========================

def creer_tache_notion(titre, date_echeance=None, priorite="Moyenne", notes=None):
    """Crée une tâche dans la base de données Notion Agenda & Tâches."""
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
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
    
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties
    }
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload)
    data = resp.json()
    if "id" in data:
        page_url = data.get("url", "")
        return data["id"], page_url
    return None, data.get("message", "Erreur inconnue")

def creer_evenement_calendar(titre, date_str, heure_str=None, description=None):
    """Crée un événement dans Google Calendar."""
    service = get_google_service('calendar', 'v3')
    if not service:
        return False
    
    try:
        event = {
            "summary": titre,
            "description": description or ""
        }
        
        if heure_str:
            dt_start = f"{date_str}T{heure_str}:00"
            dt_end_obj = datetime.datetime.strptime(f"{date_str} {heure_str}", "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)
            dt_end = dt_end_obj.strftime("%Y-%m-%dT%H:%M:00")
            event["start"] = {"dateTime": dt_start, "timeZone": "America/Toronto"}
            event["end"] = {"dateTime": dt_end, "timeZone": "America/Toronto"}
        else:
            start_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            end_obj = start_obj + datetime.timedelta(days=1)
            event["start"] = {"date": date_str}
            event["end"] = {"date": end_obj.strftime("%Y-%m-%d")}
            
        service.events().insert(calendarId='primary', body=event).execute()
        return True
    except Exception:
        return False

# ======================== FIN NOTION + CALENDAR ========================

def envoyer_facture_zoho_api(invoice_id):
    """Demande à Zoho d'envoyer la facture par email au client enregistré."""
    params = {"organization_id": ZOHO_ORG_ID}
    resp = requests.post(f"{ZOHO_BASE_URL}/invoices/{invoice_id}/email",
                         headers=zoho_headers(), params=params, json={})
    data = resp.json()
    return data.get("code") == 0  # 0 = succès chez Zoho

async def process_intent(update, intent_data):
    intent = intent_data.get("intent")
    reply = intent_data.get("reply")
    
    if reply:
        await update.message.reply_text(reply)

    if intent == "read_emails":
        await lire_emails(update, None)
        
    elif intent == "send_email":
        destinataire = intent_data.get("email_address")
        nom_contact = intent_data.get("contact_name")
        sujet = intent_data.get("email_subject", "Message de mon assistant")
        corps = intent_data.get("email_body")
        
        # Si on n'a pas l'adresse exacte mais qu'on a un nom, on cherche dans les contacts
        if not destinataire and nom_contact:
            await update.message.reply_text(f"🔍 Recherche de l'email de '{nom_contact}' dans vos contacts...")
            destinataire = await chercher_contact(nom_contact)
            
            if not destinataire:
                await update.message.reply_text(f"❌ Je n'ai pas trouvé l'adresse email de '{nom_contact}' dans vos contacts Google.")
                return
            else:
                await update.message.reply_text(f"✅ Contact trouvé : {destinataire}")
        
        if not destinataire or not corps:
            await update.message.reply_text("🤔 Il me manque l'adresse email ou le contenu du message pour pouvoir l'envoyer.")
            return
            
        # Préparation de l'email en attente et affichage boutons
        user_id = update.effective_user.id
        type_sig = intent_data.get("signature_type", "client")
        pending_emails[user_id] = {
            "destinataire": destinataire,
            "sujet": sujet,
            "corps": corps,
            "contact_name": nom_contact,
            "type_signature": type_sig
        }
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Envoyer", callback_data="send_email_approve"),
                InlineKeyboardButton("✏️ Modifier", callback_data="send_email_edit")
            ],
            [
                InlineKeyboardButton("❌ Annuler", callback_data="send_email_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Calcul de la signature qui sera utilisée à l'envoi
        SIGNATURE_CLIENT = "Cordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"
        SIGNATURE_FACTURATION = "Cordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313"
        signature_preview = SIGNATURE_FACTURATION if type_sig == "facturation" else SIGNATURE_CLIENT
        
        nom_affichage = nom_contact if nom_contact else destinataire
        preview_text = f"📝 <b>Prévisualisation de l'email pour {nom_affichage}</b>\n\n"
        preview_text += f"<b>À :</b> {destinataire}\n"
        preview_text += f"<b>Sujet :</b> {sujet}\n"
        preview_text += f"<b>Message :</b>\n{corps}\n\n"
        preview_text += f"<code>{signature_preview}</code>\n\n"
        preview_text += "<i>Voulez-vous envoyer ce message ?</i>"
        
        await update.message.reply_html(preview_text, reply_markup=reply_markup)
        
    elif intent == "send_invoice":
        client_name = intent_data.get("client_name")
        invoice_number = intent_data.get("invoice_number")
        
        if not get_zoho_token():
            await update.message.reply_text("❌ Zoho Books n'est pas encore connecté. Veuillez exécuter zoho_auth.py d'abord.")
            return
        
        await update.message.reply_text(f"🔍 Recherche de la facture dans Zoho Books...")
        factures = chercher_facture_zoho(client_name=client_name, invoice_number=invoice_number)
        
        if not factures:
            await update.message.reply_text(f"❌ Aucune facture trouvée pour '{client_name or invoice_number}' dans Zoho Books.")
            return
        
        facture = factures[0]  # La plus récente / la plus pertinente
        invoice_id = facture.get("invoice_id")
        invoice_num = facture.get("invoice_number")
        client = facture.get("customer_name")
        montant = facture.get("total")
        devise = facture.get("currency_code", "CAD")
        statut = facture.get("status")
        date = facture.get("date")
        
        user_id = update.effective_user.id
        pending_invoices[user_id] = {"invoice_id": invoice_id, "invoice_number": invoice_num, "client": client}
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Envoyer", callback_data="send_invoice_approve"),
                InlineKeyboardButton("❌ Annuler", callback_data="send_invoice_cancel")
            ]
        ]
        preview_text = f"🧾 <b>Facture trouvée dans Zoho Books</b>\n\n"
        preview_text += f"<b>Numéro :</b> {invoice_num}\n"
        preview_text += f"<b>Client :</b> {client}\n"
        preview_text += f"<b>Montant :</b> {montant} {devise}\n"
        preview_text += f"<b>Date :</b> {date}\n"
        preview_text += f"<b>Statut :</b> {statut}\n\n"
        preview_text += "<i>Envoyer cette facture par email au client via Zoho ?</i>"
        await update.message.reply_html(preview_text, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif intent == "add_event":
        summary = intent_data.get("summary", "Nouveau rendez-vous")
        date_str = intent_data.get("date")
        time_str = intent_data.get("time")
        
        if not date_str or not time_str:
            await update.message.reply_text("🤔 Je n'ai pas bien compris la date ou l'heure pour ce rendez-vous.")
            return

        user_id = update.effective_user.id
        pending_events[user_id] = {
            "summary": summary,
            "date": date_str,
            "time": time_str
        }

        keyboard = [[
            InlineKeyboardButton("✅ Confirmer", callback_data="event_approve"),
            InlineKeyboardButton("❌ Annuler", callback_data="event_cancel")
        ]]

        preview = f"📅 <b>Nouveau rendez-vous</b>\n\n"
        preview += f"<b>Titre :</b> {summary}\n"
        preview += f"<b>Date :</b> {date_str} à {time_str}\n"
        preview += f"<b>Durée :</b> 1 heure\n"
        preview += "\n<i>Ajouter ce rendez-vous à Google Calendar ?</i>"

        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))

    elif intent == "create_task":
        titre = intent_data.get("titre")
        if not titre:
            await update.message.reply_text("🤔 Je n'ai pas bien compris le titre de la tâche. Pouvez-vous préciser ?")
            return

        date_str = intent_data.get("date")
        heure_str = intent_data.get("heure")
        priorite = intent_data.get("priorite", "Moyenne")
        notes = intent_data.get("notes")

        user_id = update.effective_user.id
        pending_tasks[user_id] = {
            "titre": titre,
            "date": date_str,
            "heure": heure_str,
            "priorite": priorite,
            "notes": notes
        }

        keyboard = [[
            InlineKeyboardButton("✅ Créer", callback_data="task_approve"),
            InlineKeyboardButton("❌ Annuler", callback_data="task_cancel")
        ]]

        preview = f"📋 <b>Nouvelle tâche à créer</b>\n\n"
        preview += f"<b>Titre :</b> {titre}\n"
        if date_str:
            preview += f"<b>Date :</b> {date_str}"
            if heure_str:
                preview += f" à {heure_str}"
            preview += "\n"
        preview += f"<b>Priorité :</b> {priorite}\n"
        if notes:
            preview += f"<b>Notes :</b> {notes}\n"
        preview += "\n<i>Créer cette tâche dans Notion et Google Calendar ?</i>"

        await update.message.reply_html(preview, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "send_email_approve":
        if user_id in pending_emails:
            email_data = pending_emails[user_id]
            nom = email_data.get('contact_name') or email_data['destinataire']
            # On passe update qui contient le callback query pour les messages d'erreur éventuels
            success = await envoyer_email(update, email_data["destinataire"], email_data["sujet"], email_data["corps"], email_data.get("type_signature", "client"))
            
            if success:
                await query.edit_message_text(text=f"✈️ ✅ L'email pour {nom} a été envoyé avec succès !")
            # On supprime dans tous les cas pour éviter un multi-clic
            del pending_emails[user_id]
        else:
            await query.edit_message_text(text="⚠️ Cet email n'est plus en attente ou a déjà été envoyé.")
            
    elif data == "send_email_edit":
        # On supprime l'email en attente SANS effacer le message de prévisualisation
        if user_id in pending_emails:
            del pending_emails[user_id]
        # On envoie un NOUVEAU message pour les instructions (sans modifier la prévisualisation)
        await query.message.reply_html(
            "✏️ <b>Mode modification</b>\n"
            "Je vois toujours le brouillon ci-dessus. Dites-moi simplement ce que vous voulez changer :\n"
            "<i>Ex: 'Rends-le plus formel', 'Ajoute que le délai est vendredi'</i>"
        )
        
    elif data == "send_email_cancel":
        if user_id in pending_emails:
            del pending_emails[user_id]
        await query.edit_message_text(text="❌ Envoi de l'email annulé.")

    elif data == "send_invoice_approve":
        if user_id in pending_invoices:
            inv = pending_invoices[user_id]
            await query.edit_message_text(text=f"🔄 Envoi de la facture #{inv['invoice_number']} à {inv['client']} via Zoho...")
            success = envoyer_facture_zoho_api(inv["invoice_id"])
            if success:
                await query.edit_message_text(text=f"✅ Facture #{inv['invoice_number']} envoyée avec succès à {inv['client']} !")
            else:
                await query.edit_message_text(text=f"❌ Échec de l'envoi de la facture. Vérifiez dans Zoho Books.")
            del pending_invoices[user_id]
        else:
            await query.edit_message_text(text="⚠️ Cette facture n'est plus en attente.")

    elif data == "send_invoice_cancel":
        if user_id in pending_invoices:
            del pending_invoices[user_id]
        await query.edit_message_text(text="❌ Envoi de la facture annulé.")

    elif data == "task_approve":
        if user_id in pending_tasks:
            t = pending_tasks[user_id]
            msgs = []
            
            # Création Notion
            task_id, notion_url = creer_tache_notion(
                titre=t["titre"],
                date_echeance=t.get("date"),
                priorite=t.get("priorite", "Moyenne"),
                notes=t.get("notes")
            )
            if task_id:
                msgs.append(f"✅ Tâche créée dans Notion")
            else:
                msgs.append(f"⚠️ Notion : {notion_url}")
            
            # Création Calendar
            if t.get("date"):
                ok = creer_evenement_calendar(
                    titre=f"📋 {t['titre']}",
                    date_str=t["date"],
                    heure_str=t.get("heure"),
                    description=t.get("notes")
                )
                msgs.append("✅ Événement ajouté dans Google Calendar" if ok else "⚠️ Échec ajout Calendar")
            
            del pending_tasks[user_id]
            await query.edit_message_text(text="\n".join(msgs))
        else:
            await query.edit_message_text(text="⚠️ Cette tâche n'est plus en attente.")

    elif data == "task_cancel":
        if user_id in pending_tasks:
            del pending_tasks[user_id]
        await query.edit_message_text(text="❌ Création de tâche annulée.")

    elif data == "event_approve":
        if user_id in pending_events:
            ev = pending_events[user_id]
            try:
                service = get_google_service('calendar', 'v3')
                if not service:
                    await query.edit_message_text(text="❌ Erreur: Aucune connexion à Google Calendar.")
                    return
                start_dt = f"{ev['date']}T{ev['time']}:00-04:00"
                end_dt_obj = datetime.datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)
                end_dt = end_dt_obj.strftime("%Y-%m-%dT%H:%M:00-04:00")
                event_body = {
                    'summary': ev['summary'],
                    'start': {'dateTime': start_dt},
                    'end': {'dateTime': end_dt},
                }
                service.events().insert(calendarId='primary', body=event_body).execute()
                del pending_events[user_id]
                await query.edit_message_text(text=f"📅 ✅ Rendez-vous '{ev['summary']}' ajouté avec succès à votre <b>Google Calendar</b> !\n\n⚠️ Si vous ne le voyez pas dans l'app Mac Calendar, cochez <b>awelldonestudio@gmail.com</b> dans la sidebar gauche.")
            except Exception as e:
                await query.edit_message_text(text=f"⚠️ Erreur Calendar : {str(e)}")
        else:
            await query.edit_message_text(text="⚠️ Ce rendez-vous n'est plus en attente.")

    elif data == "event_cancel":
        if user_id in pending_events:
            del pending_events[user_id]
        await query.edit_message_text(text="❌ Rendez-vous annulé.")

# ======================== AGENT VOYAGE (COMMANDE /voyage) ========================
async def cmd_voyage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # context.args contient les mots après la commande /voyage
    if not context.args:
        await update.message.reply_text("✈️ Veuillez préciser votre demande après `/voyage`. Ex: `/voyage Paris le 14 mai`", parse_mode="Markdown")
        return
        
    requete_voyage = " ".join(context.args)
    
    # Mémoire
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": requete_voyage})
    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]
        
    await update.message.reply_text("🌍 Recherche et optimisation en cours... Cela peut prendre quelques instants.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    try:
        reponse_voyage = await handle_voyage_request(openai_client, user_conversations[user_id])
        user_conversations[user_id].append({"role": "assistant", "content": reponse_voyage})
        if len(user_conversations[user_id]) > 10:
            user_conversations[user_id] = user_conversations[user_id][-10:]
            
        max_len = 4096
        for i in range(0, len(reponse_voyage), max_len):
            await update.message.reply_text(reponse_voyage[i:i+max_len], parse_mode=None)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur de l'agent voyage : {e}")
# =========================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texte = update.message.text
    user_id = update.effective_user.id
    
    # Gestion de la mémoire de conversation globale (partagée avec le bot principal)
    if user_id not in user_conversations:
        user_conversations[user_id] = []
        
    # On ajoute le nouveau message
    user_conversations[user_id].append({"role": "user", "content": texte})
    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    # ----------------------------------------------------
    # FONCTIONNALITÉ SPÉCIALE : AGENT DE VOYAGE INTÉGRÉ
    # ----------------------------------------------------
    if texte.strip().startswith(":voyage"):
        requete_voyage = texte[len(":voyage"):].strip()
        if not requete_voyage and len(user_conversations[user_id]) <= 1:
            await update.message.reply_text("✈️ Veuillez préciser votre demande après `:voyage`.")
            return
            
        await update.message.reply_text("🌍 Recherche et optimisation en cours... Cela peut prendre quelques instants.")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        try:
            # On passe l'entiereté du contexte de conversation (les 10 messages max)
            reponse_voyage = await handle_voyage_request(openai_client, user_conversations[user_id])
            
            # On stocke sa réponse pour les futures itérations du voyage
            user_conversations[user_id].append({"role": "assistant", "content": reponse_voyage})
            if len(user_conversations[user_id]) > 10:
                user_conversations[user_id] = user_conversations[user_id][-10:]
            
            # Découper le message si supérieur à 4096 char pour Telegram
            max_len = 4096
            for i in range(0, len(reponse_voyage), max_len):
                await update.message.reply_text(reponse_voyage[i:i+max_len], parse_mode=None)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Erreur de l'agent voyage : {e}")
        return

    # ----------------------------------------------------
    
    if not openai_client:
        await update.message.reply_text("⚠️ Je n'ai pas de clé OpenAI configurée dans le fichier .env.")
        return
        
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    day_name = now.strftime("%A")
    
    system_prompt = f"""
    Tu es l'assistant personnel très intelligent de l'utilisateur sur son Telegram.
    Aujourd'hui, nous sommes le {day_name} {now_str}.
    Tu as accès aux actions concrètes suivantes sur internet :
    - Lire les emails (intent: "read_emails")
    - Envoyer des emails (intent: "send_email")
    - Ajouter des événements au calendrier (intent: "add_event")
    - Envoyer une facture depuis Zoho Books (intent: "send_invoice")
    - Créer une tâche dans Notion ET Google Calendar (intent: "create_task")
    
    L'utilisateur va te parler. Si sa demande correspond à une action que tu sais faire, extrais les informations et utilise le bon "intent".
    Sinon, utilise l'intent "chat" pour discuter normalement ou demander des précisions.
    
    RÈGLE DE TON POUR LES EMAILS :
    - Par défaut (signature_type = "client"), rédige en TUTOIEMENT. Ton simple, naturel et professionnel.
    - Si signature_type = "facturation", utilise le VOUVOIEMENT obligatoirement. Ton plus formel, sobre et précis. Éviter toute familiarité. Phrases courtes et directes. Aucune formule chaleureuse excessive.
    - Si l'utilisateur mentionne explicitement de vouvoyer dans un message client, alors passer au vouvoiement pour cet email uniquement.
    - Cette règle s'applique uniquement à email_body, pas à la réponse dans "reply".
    
    RÈGLE DE STYLE D'ÉCRITURE :
    - Le style doit être professionnel, CHALEUREUX, POLI et empathique.
    - Toujours commencer par une salutation appropriée (ex: "Bonjour [Prénom]," ou "Salut [Prénom],").
    - Toujours inclure une petite phrase de contexte sympathique si pertinent (ex: "J'espère que tu vas bien.").
    - Ne jamais être trop direct ou sec. Amener les demandes (surtout de paiement) avec tact.
    - Éviter les formulations trop orales : "car on a", "juste pour dire", "je voulais juste", "est-ce que tu pourrais juste".
    - Le message ne doit jamais paraître généré par une IA ni être excessivement formel.
    - Toujours terminer par une formule de politesse chaleureuse (ex: "Merci d'avance pour ton aide !", "Bonne fin de journée !", "À très vite,").
    - Objectif : donner l'impression d'avoir été écrit calmement par un humain poli et amical.
    
    IMPORTANT: Réponds TOUJOURS avec un objet JSON strict et valide contenant :
    - "intent" : l'action à réaliser ("read_emails", "send_email", "add_event", "send_invoice", "create_task", ou "chat").
    - "reply" : Une réponse naturelle courte pour l'utilisateur.
    
    RÈGLE CRITIQUE - DIFFÉRENCIER TÂCHE vs ÉVÉNEMENT :
    - "add_event" = UNIQUEMENT un rendez-vous/réunion avec une HEURE PRÉCISE explicitement mentionnée (ex: "réunion lundi à 14h", "call mardi à 10h30"). Si aucune heure n'est donnée, ce n'est PAS un add_event.
    - "create_task" = Une action à faire, une chose à préparer, un suivi, un rappel — même si une date est mentionnée sans heure (ex: "envoyer le devis", "préparer le rapport pour vendredi").
    - Si la demande est AMBIGUË (ex: "ajouter une chose pour lundi" — impossible de savoir si c'est une tâche ou un rendez-vous), utiliser l'intent "chat" et poser une question courte : "Souhaitez-vous créer une tâche dans Notion, ou un rendez-vous dans votre agenda ?"
    - Ne jamais inventer ou supposer — mieux vaut demander que se tromper.

    SI intent est "add_event", fournis :
    - "summary" : Titre
    - "date" : YYYY-MM-DD
    - "time" : HH:MM (24h)
    
    SI intent est "send_email", fournis :
    - "email_address" : L'adresse email complète du destinataire SEULEMENT SI l'utilisateur l'a donnée explicitement (ex: jean@gmail.com). Sinon, null.
    - "contact_name": Le prénom ou le nom de la personne à qui envoyer le mail SI l'adresse n'est pas fournie (ex: "Andréanne"). Le système cherchera l'adresse dans les carnets d'adresses.
    - "email_subject" : Le sujet du mail
    - "email_body" : Le contenu complet du message rédigé avec professionnalisme. NE PAS signer le message dans ce champ, la signature est gérée automatiquement.
    - "signature_type" : Choisir STRICTEMENT selon ces cas :
       → "facturation" UNIQUEMENT si l'email est : (1) l'envoi officiel d'une facture, ou (2) un rappel de paiement formel sur une facture déjà émise.
       → "client" pour TOUT le reste, y compris : une question à propos d'un paiement, une discussion sur une facture tierce, une demande de confirmation de réception de paiement, une coordination ou tout échange relationnel même s'il mentionne de l'argent ou une facture.
    
    RÈGLE DE SIGNATURE ABSOLUE (CRITIQUE) :
    - La signature est TOUJOURS ajoutée automatiquement par le système. NE JAMAIS inclure dans email_body : 'Cordialement', 'Salutations', 'Bonne journée', un nom, un numéro de téléphone, ou toute autre formule de clôture. Le corps doit se terminer par la dernière phrase de texte du message.
    - Signer toujours au nom de l'entreprise (Jean-Philippe Roy Tanguay OU Facturation), JAMAIS en tant qu'IA, bot, ou assistant.
    - En cas de doute, utiliser "client".
    
    SI intent est "send_invoice", fournis :
    - "client_name" : le nom du client tel que mentionné par l'utilisateur (ex: "Playground", "Andréanne"). Sinon, null.
    - "invoice_number" : le numéro de facture si mentionné explicitement (ex: "INV-001", "facture #5"). Sinon, null.
    (Le système cherchera la facture dans Zoho Books et demandera confirmation avant l'envoi.)
    
    SI intent est "create_task", fournis :
    - "titre" : Le titre de la tâche (obligatoire)
    - "date" : Date d'échéance format YYYY-MM-DD (si mentionnée, sinon null)
    - "heure" : Heure format HH:MM (si mentionnée, sinon null)
    - "priorite" : "Haute", "Moyenne" ou "Basse" (déduire selon le contexte, défaut "Moyenne")
    - "notes" : Notes supplémentaires (si pertinent, sinon null)
    """
    
    messages_payload = [{"role": "system", "content": system_prompt}] + user_conversations[user_id]
    
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages_payload,
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        intent_data = json.loads(content)
        
        # On sauvegarde la réponse de l'IA dans l'historique
        assistant_memory = intent_data.get("reply", "")
        if intent_data.get("intent") == "send_email":
            # On donne le contexte du brouillon préparé à l'IA pour qu'elle s'en souvienne si l'utilisateur veut le modifier
            sujet_draft = intent_data.get("email_subject", "")
            corps_draft = intent_data.get("email_body", "")
            assistant_memory += f"\n[BROUILLON PRÉPARÉ - Sujet: {sujet_draft} | Corps: {corps_draft}]"
            
        user_conversations[user_id].append({"role": "assistant", "content": assistant_memory})
        
        await process_intent(update, intent_data)
        
    except Exception as e:
         await update.message.reply_text(f"⚠️ Le cerveau OpenAI a eu un bug de réflexion : {str(e)}")

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("emails", lire_emails))
    application.add_handler(CommandHandler("voyage", cmd_voyage)) # NOUVELLE COMMANDE TELEGRAM NATIVE
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    print("🚀 Le bot Telegram V4 (Google + OpenAI + Agent Voyage) est en ligne !")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
