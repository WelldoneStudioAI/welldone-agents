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
# Supporte NOTION_TOKEN (legacy) et NOTION_API_KEY (Railway actuel)
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN", "") or os.environ.get("NOTION_API_KEY", "")
NOTION_TASK_DB    = os.environ.get("NOTION_TASK_DB", "bd4cff932b7842b19f7cb748e1abda48")
NOTION_SOURCES_DB = os.environ.get("NOTION_SOURCES_DB", "10e87f01b8ec4c60a6383d48c7aac4a2")

# ── QuickBooks Online ─────────────────────────────────────────────────────────
QBO_CLIENT_ID      = os.environ.get("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET  = os.environ.get("QBO_CLIENT_SECRET", "")
QBO_REFRESH_TOKEN  = os.environ.get("QBO_REFRESH_TOKEN", "")
QBO_REALM_ID       = os.environ.get("QBO_REALM_ID", "")
QBO_SERVICE_ITEM_ID = os.environ.get("QBO_SERVICE_ITEM_ID", "1")
QBO_BASE_URL       = os.environ.get("QBO_BASE_URL", "https://quickbooks.api.intuit.com/v3/company")

# ── Google Sheets (tenue de livres) ───────────────────────────────────────────
SHEETS_LIVRES_ID   = os.environ.get("SHEETS_LIVRES_ID", "")

# ── Email ─────────────────────────────────────────────────────────────────────
GMAIL_RECIPIENT    = os.environ.get("GMAIL_RECIPIENT", "awelldonestudio@gmail.com")
EMAIL_FROM_JP      = "jptanguay@awelldone.studio"
EMAIL_FROM_BILL    = "billing@awelldone.studio"
EMAIL_BCC          = "ia@awelldone.studio"
GMAIL_BILLING_FROM = os.environ.get("GMAIL_BILLING_FROM", "facturation@awelldone.studio")

# ── QBO Facturation ───────────────────────────────────────────────────────────
QBO_BILLING_SIGNATURE = "Service Externe Comptabilité Welldone Studio"

# ── Google Analytics ──────────────────────────────────────────────────────────
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "522467276")

# ── Search Console ────────────────────────────────────────────────────────────
GSC_SITE_STUDIO = "sc-domain:awelldone.studio"
GSC_SITE_ARCHI  = "sc-domain:welldone.archi"

# ── OpenAI (Agent Voyage — GPT-4o + function calling) ────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ── SerpAPI (conservé en fallback) ───────────────────────────────────────────
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# ── Amadeus (Flight Offers — inclut Air Transat, WestJet, Air Canada + hubs) ──
AMADEUS_API_KEY    = os.environ.get("AMADEUS_API_KEY", "")
AMADEUS_API_SECRET = os.environ.get("AMADEUS_API_SECRET", "")
# test = sandbox gratuit | production = données réelles (2000 appels/mois gratuit)
AMADEUS_BASE_URL   = os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")

# ── Framer CMS (Blog awelldone.studio) ───────────────────────────────────────
FRAMER_API_KEY       = os.environ.get("FRAMER_API_KEY", "fr_2xsx07vykt81c9y2p0krj2xgmk")
FRAMER_COLLECTION_ID = os.environ.get("FRAMER_COLLECTION_ID", "ERDJzzQHr")  # Welldone Studio-Blog
# Collection des projets (pour les photos de portfolio dans les articles de blog)
# → Lancer `framer collections` dans Telegram pour trouver l'ID correct
FRAMER_PROJECTS_COLLECTION_ID = os.environ.get("FRAMER_PROJECTS_COLLECTION_ID", "")
# Unsplash Developer API (gratuit — https://unsplash.com/developers)
# Ajouter UNSPLASH_ACCESS_KEY dans Railway pour des images de qualité
UNSPLASH_ACCESS_KEY  = os.environ.get("UNSPLASH_ACCESS_KEY", "")

# ── WHC Email (IMAP/SMTP — awelldone.com) ────────────────────────────────────
WHC_IMAP_HOST = os.environ.get("WHC_IMAP_HOST", "mail.awelldone.com")
WHC_IMAP_PORT = int(os.environ.get("WHC_IMAP_PORT", "993"))
WHC_SMTP_HOST = os.environ.get("WHC_SMTP_HOST", "mail.awelldone.com")
WHC_SMTP_PORT = int(os.environ.get("WHC_SMTP_PORT", "465"))
WHC_EMAIL     = os.environ.get("WHC_EMAIL",    "jptanguay@awelldone.com")
WHC_PASSWORD  = os.environ.get("WHC_PASSWORD", "")

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/Toronto"
