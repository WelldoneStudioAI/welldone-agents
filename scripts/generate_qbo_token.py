#!/usr/bin/env python3
"""
scripts/generate_qbo_token.py — Génère les tokens OAuth 2.0 QuickBooks Online.

Usage:
  python3 scripts/generate_qbo_token.py

Prérequis:
  1. Créer une app sur developer.intuit.com
  2. Ajouter http://localhost:8080/callback comme Redirect URI dans l'app
  3. Avoir QBO_CLIENT_ID et QBO_CLIENT_SECRET disponibles

Ce script:
  1. Ouvre le browser → page de consentement Intuit
  2. Lance un serveur HTTP local pour capturer le callback
  3. Échange le code d'autorisation contre access_token + refresh_token
  4. Affiche les 4 valeurs à copier dans Railway

Variables Railway à configurer ensuite:
  QBO_CLIENT_ID     ← ton Client ID Intuit
  QBO_CLIENT_SECRET ← ton Client Secret Intuit
  QBO_REFRESH_TOKEN ← affiché par ce script
  QBO_REALM_ID      ← Company ID visible dans l'URL QuickBooks (?realmId=...)
"""
import sys, os, json, urllib.parse, webbrowser, secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from base64 import b64encode

# ── Configuration ──────────────────────────────────────────────────────────────
REDIRECT_URI   = "http://localhost:8080/callback"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTH_ENDPOINT  = "https://appcenter.intuit.com/connect/oauth2"
SCOPES         = "com.intuit.quickbooks.accounting"

# ── Variables globales pour le serveur HTTP ────────────────────────────────────
_auth_code  = None
_state_recv = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _state_recv
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _auth_code  = params["code"][0]
            _state_recv = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;text-align:center;padding:50px">
                <h2>&#10003; Autorisation accordée !</h2>
                <p>Tu peux fermer cette fenêtre et revenir dans le terminal.</p>
                </body></html>
            """)
        else:
            error = params.get("error", ["inconnu"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body><h2>Erreur: {error}</h2></body></html>".encode())

    def log_message(self, format, *args):
        pass  # Silencer les logs HTTP


def get_credentials() -> tuple[str, str]:
    client_id     = os.environ.get("QBO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("QBO_CLIENT_SECRET", "").strip()

    if not client_id:
        client_id = input("QBO_CLIENT_ID (developer.intuit.com → ton app) : ").strip()
    if not client_secret:
        client_secret = input("QBO_CLIENT_SECRET : ").strip()

    if not client_id or not client_secret:
        print("❌ Client ID et Client Secret requis.")
        sys.exit(1)

    return client_id, client_secret


def main():
    print("\n" + "═" * 55)
    print("  QuickBooks Online — Génération du token OAuth 2.0")
    print("═" * 55 + "\n")

    client_id, client_secret = get_credentials()

    # Générer un state aléatoire
    state = secrets.token_urlsafe(16)

    # Construire l'URL d'autorisation
    auth_params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPES,
        "state":         state,
    }
    auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(auth_params)

    print("1. Ouverture du browser pour autoriser l'accès à QuickBooks...")
    print(f"   URL: {auth_url}\n")
    webbrowser.open(auth_url)

    # Lancer le serveur HTTP local
    print("2. En attente du callback sur http://localhost:8080/callback...")
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()  # Une seule requête

    if not _auth_code:
        print("❌ Aucun code d'autorisation reçu.")
        sys.exit(1)

    if _state_recv != state:
        print(f"⚠️  State mismatch! Attendu: {state}, reçu: {_state_recv}")
        sys.exit(1)

    print(f"✅ Code d'autorisation reçu.\n")

    # Échanger le code contre des tokens
    print("3. Échange du code contre les tokens...")
    import requests

    credentials = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_ENDPOINT,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "Accept":        "application/json",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         _auth_code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )

    if resp.status_code != 200:
        print(f"❌ Erreur token exchange: {resp.status_code} — {resp.text}")
        sys.exit(1)

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")
    access_token  = tokens.get("access_token", "")

    if not refresh_token:
        print(f"❌ Pas de refresh_token dans la réponse: {tokens}")
        sys.exit(1)

    print("✅ Tokens obtenus!\n")

    # Récupérer le Realm ID depuis l'URL QBO (ou demander)
    realm_id = os.environ.get("QBO_REALM_ID", "").strip()
    if not realm_id:
        print("4. Realm ID (Company ID) :")
        print("   → Connecte-toi à QuickBooks Online")
        print("   → L'URL contient : ?realmId=XXXXXXXXXX")
        realm_id = input("   QBO_REALM_ID : ").strip()

    print("\n" + "═" * 55)
    print("  COPIE CES 4 VARIABLES DANS RAILWAY")
    print("═" * 55)
    print(f"\nQBO_CLIENT_ID     = {client_id}")
    print(f"QBO_CLIENT_SECRET = {client_secret}")
    print(f"QBO_REFRESH_TOKEN = {refresh_token}")
    print(f"QBO_REALM_ID      = {realm_id}")
    print("\n" + "═" * 55)
    print("  Railway → welldone-agents → Variables → + New Variable")
    print("═" * 55 + "\n")

    # Sauvegarder localement pour référence
    output = {
        "QBO_CLIENT_ID":     client_id,
        "QBO_CLIENT_SECRET": client_secret,
        "QBO_REFRESH_TOKEN": refresh_token,
        "QBO_REALM_ID":      realm_id,
        "access_token":      access_token,
    }
    out_file = "qbo_tokens.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2)
    print(f"💾 Tokens sauvegardés localement dans {out_file}")
    print("⚠️  Ne commite JAMAIS ce fichier — il est dans .gitignore\n")


if __name__ == "__main__":
    main()
