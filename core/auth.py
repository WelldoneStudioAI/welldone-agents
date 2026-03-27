"""
core/auth.py — Centralise TOUTE la gestion des tokens.

Deux types d'auth Google :
  1. Service Account  → GA4, Search Console (jamais d'expiration)
  2. OAuth credentials → Gmail, Calendar, Contacts (refresh automatique)

Fallback DEV : si les variables d'env Railway ne sont pas définies,
tente de charger les tokens depuis ~/.config/gws/ (tokens locaux existants).

Aucun autre fichier ne doit reconstruire de tokens ou lire GOOGLE_* directement.
"""
import base64, json, os, tempfile, logging
from pathlib import Path

log = logging.getLogger(__name__)

# Chemins temporaires (valides pour la durée de vie du process)
_TMP = Path(tempfile.gettempdir())
_SA_JSON_PATH    = _TMP / "gws_service_account.json"
_OAUTH_JSON_PATH = _TMP / "gws_oauth_token.json"
_ZOHO_JSON_PATH  = _TMP / "zoho_token.json"

# Chemin des tokens locaux (dev/transition)
_LOCAL_TOKENS = Path.home() / ".config" / "gws"

# Cache en mémoire des objets credentials
_sa_creds    = None
_oauth_creds = None


# ── Service Account ────────────────────────────────────────────────────────────

def get_service_account_creds(scopes: list[str]):
    """
    Retourne les credentials Service Account Google pour les scopes donnés.

    Priorité :
      1. GOOGLE_SA_JSON_B64 (Railway / production)
      2. Fichiers OAuth locaux dans ~/.config/gws/ (DEV — transition)
    """
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    b64 = os.environ.get("GOOGLE_SA_JSON_B64", "")
    if b64:
        if not _SA_JSON_PATH.exists():
            _SA_JSON_PATH.write_text(base64.b64decode(b64).decode())
            log.info("✅ Service Account JSON reconstruit depuis env var")
        return service_account.Credentials.from_service_account_file(
            str(_SA_JSON_PATH), scopes=scopes
        )

    # ── Fallback DEV : utiliser les tokens OAuth locaux par scope ────────────
    log.warning("⚠️  GOOGLE_SA_JSON_B64 non défini — fallback vers tokens OAuth locaux (dev)")

    # Mapper les scopes vers les fichiers locaux existants
    scope_to_file = {
        "https://www.googleapis.com/auth/analytics.readonly":  "analytics_token.json",
        "https://www.googleapis.com/auth/webmasters.readonly":  "searchconsole_token.json",
    }
    for scope in scopes:
        local_file = _LOCAL_TOKENS / scope_to_file.get(scope, "")
        if local_file.exists():
            creds = Credentials.from_authorized_user_file(str(local_file), [scope])
            if not creds.valid and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return creds

    raise RuntimeError(
        "GOOGLE_SA_JSON_B64 non défini et aucun token local trouvé dans ~/.config/gws/\n"
        "→ Voir MIGRATION.md Étape 1 pour créer le Service Account."
    )


# ── OAuth (Gmail / Calendar / Contacts) ───────────────────────────────────────

def get_oauth_creds():
    """
    Retourne les credentials OAuth Google (Gmail, Calendar, Contacts).
    Utilise GOOGLE_OAUTH_JSON (JSON brut du token, pas base64).
    Rafraîchit automatiquement si le token est expiré.
    """
    global _oauth_creds
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if _oauth_creds and _oauth_creds.valid:
        return _oauth_creds

    raw = os.environ.get("GOOGLE_OAUTH_JSON", "")
    if not raw:
        # Fallback DEV : utiliser gmail_send_token local
        local_gmail = _LOCAL_TOKENS / "gmail_send_token.json"
        if local_gmail.exists():
            log.warning("⚠️  GOOGLE_OAUTH_JSON non défini — fallback vers gmail_send_token.json (dev)")
            raw = local_gmail.read_text()
        else:
            raise RuntimeError(
                "GOOGLE_OAUTH_JSON non défini — impossible de charger les credentials OAuth\n"
                "→ Voir MIGRATION.md Étape 2 pour générer le token OAuth."
            )

    token_data = json.loads(raw)
    _oauth_creds = Credentials.from_authorized_user_info(token_data)

    if not _oauth_creds.valid:
        if _oauth_creds.expired and _oauth_creds.refresh_token:
            _oauth_creds.refresh(Request())
            log.info("🔄 Token OAuth Google rafraîchi")
            # Mettre à jour la variable d'environnement en mémoire pour ce process
            os.environ["GOOGLE_OAUTH_JSON"] = _oauth_creds.to_json()
        else:
            raise RuntimeError("Token OAuth Google invalide et non rafraîchissable")

    return _oauth_creds


def get_google_service(api_name: str, api_version: str, use_service_account: bool = False, scopes: list | None = None):
    """
    Crée et retourne un client d'API Google.

    Args:
        api_name: Ex: 'gmail', 'calendar', 'analyticsdata'
        api_version: Ex: 'v1', 'v3', 'v1beta'
        use_service_account: True pour GA4 / GSC, False pour Gmail / Calendar
        scopes: Requis si use_service_account=True
    """
    from googleapiclient.discovery import build

    if use_service_account:
        creds = get_service_account_creds(scopes or [])
    else:
        creds = get_oauth_creds()

    return build(api_name, api_version, credentials=creds)


# ── Zoho Books ─────────────────────────────────────────────────────────────────

def get_zoho_access_token() -> str:
    """
    Retourne un access token Zoho valide.
    Utilise le refresh_token pour en obtenir un nouveau si nécessaire.
    """
    import time, requests

    # Charger le token depuis le fichier temp (s'il existe et est valide)
    if _ZOHO_JSON_PATH.exists():
        data = json.loads(_ZOHO_JSON_PATH.read_text())
        if data.get("expires_at", 0) > time.time() + 60:
            return data["access_token"]

    # Refresh
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN", "")
    client_id     = os.environ.get("ZOHO_CLIENT_ID", "")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET", "")

    if not all([refresh_token, client_id, client_secret]):
        raise RuntimeError("Variables Zoho manquantes : ZOHO_REFRESH_TOKEN, ZOHO_CLIENT_ID ou ZOHO_CLIENT_SECRET")

    resp = requests.post(
        "https://accounts.zoho.ca/oauth/v2/token",
        params={
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    token_data = resp.json()

    if "access_token" not in token_data:
        raise RuntimeError(f"Zoho refresh failed: {token_data}")

    token_data["expires_at"] = time.time() + int(token_data.get("expires_in", 3600)) - 60
    _ZOHO_JSON_PATH.write_text(json.dumps(token_data))
    log.info("🔄 Token Zoho rafraîchi")

    return token_data["access_token"]


# ── Notion ─────────────────────────────────────────────────────────────────────

def get_notion_headers() -> dict:
    """Retourne les headers HTTP pour l'API Notion."""
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        raise RuntimeError("NOTION_TOKEN non défini")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
