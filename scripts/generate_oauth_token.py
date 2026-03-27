#!/usr/bin/env python3
"""
scripts/generate_oauth_token.py — Génère le token OAuth Google une fois.

Lance ce script UNE seule fois depuis ton Mac pour obtenir le JSON OAuth.
Ce JSON va dans la variable GOOGLE_OAUTH_JSON sur Railway.

Prérequis :
  - Créer un OAuth 2.0 Client ID dans Google Cloud Console
  - Télécharger le client_secret.json depuis GCC
  - Activer les APIs : Gmail API, Calendar API, People API

Usage :
  python scripts/generate_oauth_token.py --client-secret path/to/client_secret.json
"""
import sys, json, argparse
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
except ImportError:
    print("❌ Installez les dépendances d'abord :")
    print("   pip install google-auth-oauthlib google-auth")
    sys.exit(1)

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-secret", required=True, help="Chemin vers client_secret.json")
    parser.add_argument("--output", default="oauth_token.json", help="Fichier de sortie")
    args = parser.parse_args()

    client_secret = Path(args.client_secret)
    if not client_secret.exists():
        print(f"❌ Fichier introuvable: {client_secret}")
        sys.exit(1)

    print("🌐 Ouverture du navigateur pour l'autorisation Google...")
    print("   → Connecte-toi avec le compte awelldone.studio")
    print("   → Accepte tous les accès demandés\n")

    flow  = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0)

    output = Path(args.output)
    output.write_text(creds.to_json())

    print(f"\n✅ Token OAuth sauvegardé dans {output}")
    print("\n" + "═" * 60)
    print("PROCHAINE ÉTAPE:")
    print("─" * 60)
    print("Copie le contenu JSON dans Railway → GOOGLE_OAUTH_JSON:")
    print(f"\n  cat {output} | pbcopy\n")
    print("Ou affiche-le directement:")
    print(f"\n  cat {output}\n")
    print("═" * 60)

    # Vérifier que le token fonctionne
    print("\n⏳ Test de connexion Gmail...")
    try:
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=creds)
        profile = svc.users().getProfile(userId="me").execute()
        print(f"✅ Gmail OK → {profile['emailAddress']}")
    except Exception as e:
        print(f"⚠️  Test Gmail échoué: {e}")

if __name__ == "__main__":
    main()
