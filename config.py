"""
config.py — Source de vérité unique pour toutes les constantes.
Toutes les variables d'environnement sont lues ici, nulle part ailleurs.
"""
import os

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "8434904512"))

# ── Claude / Anthropic ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Google Service Account (GA4 + Search Console) ────────────────────────────
# JSON base64-encodé de la clé Service Account téléchargée depuis Google Cloud
GOOGLE_SA_JSON_B64 = os.environ.get("GOOGLE_SA_JSON_B64", "")

# ── Google OAuth (Gmail + Calendar + Contacts) ────────────────────────────────
# JSON du token OAuth (contient access_token, refresh_token, client_id, client_secret)
# Généré une fois localement, stocké en Railway en tant que variable JSON brute
GOOGLE_OAUTH_JSON = os.environ.get("GOOGLE_OAUTH_JSON", "")

# Scopes OAuth nécessaires pour Gmail + Calendar + Contacts
GOOGLE_OAUTH_SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

# ── Notion ────────────────────────────────────────────────────────────────────
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN", "")
NOTION_TASK_DB    = os.environ.get("NOTION_TASK_DB", "bd4cff932b7842b19f7cb748e1abda48")
NOTION_SOURCES_DB = os.environ.get("NOTION_SOURCES_DB", "10e87f01b8ec4c60a6383d48c7aac4a2")

# ── QuickBooks Online ─────────────────────────────────────────────────────────
QBO_CLIENT_ID      = os.environ.get("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET  = os.environ.get("QBO_CLIENT_SECRET", "")
QBO_REFRESH_TOKEN  = os.environ.get("QBO_REFRESH_TOKEN", "")
QBO_REALM_ID       = os.environ.get("QBO_REALM_ID", "")
QBO_SERVICE_ITEM_ID = os.environ.get("QBO_SERVICE_ITEM_ID", "1")
QBO_BASE_URL       = "https://quickbooks.api.intuit.com/v3/company"

# ── Google Sheets (tenue de livres) ───────────────────────────────────────────
SHEETS_LIVRES_ID   = os.environ.get("SHEETS_LIVRES_ID", "")

# ── Email ─────────────────────────────────────────────────────────────────────
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", "awelldonestudio@gmail.com")
EMAIL_FROM_JP   = "jptanguay@awelldone.studio"
EMAIL_FROM_BILL = "billing@awelldone.studio"
EMAIL_BCC       = "ia@awelldone.studio"

# ── Google Analytics ──────────────────────────────────────────────────────────
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "522467276")

# ── Search Console ────────────────────────────────────────────────────────────
GSC_SITE_STUDIO = "sc-domain:awelldone.com"
GSC_SITE_ARCHI  = "sc-domain:welldone.archi"

# ── OpenAI (Agent Voyage — GPT-4o + function calling) ────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ── SerpAPI (Google Flights pour agent voyage) ────────────────────────────────
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/Toronto"
