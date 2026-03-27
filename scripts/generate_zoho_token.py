#!/usr/bin/env python3
"""
scripts/generate_zoho_token.py — Obtient le refresh_token Zoho Books (une fois).

Le refresh_token ne s'expire pas → à copier dans ZOHO_REFRESH_TOKEN sur Railway.

Prérequis :
  - Aller sur https://api-console.zoho.ca
  - Créer une "Self Client"
  - Scopes requis : ZohoBooks.fullaccess.all
  - Générer un code d'autorisation (durée: 10 minutes)

Usage :
  python scripts/generate_zoho_token.py
"""
import sys, os, json, requests

# Charger .env si présent
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CLIENT_ID     = os.environ.get("ZOHO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ ZOHO_CLIENT_ID et ZOHO_CLIENT_SECRET requis dans .env")
        sys.exit(1)

    print("═" * 60)
    print("ZOHO BOOKS — Génération du Refresh Token")
    print("═" * 60)
    print()
    print("1. Va sur: https://api-console.zoho.ca")
    print("2. Sélectionne ta Self Client")
    print("3. Clique sur 'Generate Code'")
    print("4. Scope: ZohoBooks.fullaccess.all")
    print("5. Copie le code d'autorisation (valide 10 min)")
    print()

    auth_code = input("Colle ici le code d'autorisation: ").strip()

    if not auth_code:
        print("❌ Code vide")
        sys.exit(1)

    print("\n⏳ Échange du code contre un refresh_token...")

    resp = requests.post(
        "https://accounts.zoho.ca/oauth/v2/token",
        params={
            "code":          auth_code,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri":  "https://www.zoho.com/books",
            "grant_type":    "authorization_code",
        },
        timeout=10,
    )

    data = resp.json()
    if "refresh_token" not in data:
        print(f"❌ Erreur: {json.dumps(data, indent=2)}")
        sys.exit(1)

    refresh_token = data["refresh_token"]
    print(f"\n✅ Refresh token obtenu!\n")
    print("═" * 60)
    print("PROCHAINE ÉTAPE:")
    print("─" * 60)
    print("Copie ce refresh_token dans Railway → ZOHO_REFRESH_TOKEN:")
    print(f"\n  {refresh_token}\n")
    print("═" * 60)

    # Tester
    print("\n⏳ Test de connexion Zoho Books...")
    try:
        test_resp = requests.post(
            "https://accounts.zoho.ca/oauth/v2/token",
            params={
                "refresh_token": refresh_token,
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "refresh_token",
            },
            timeout=10,
        )
        test_data = test_resp.json()
        if "access_token" in test_data:
            print(f"✅ Zoho Books OK")
        else:
            print(f"⚠️  Test échoué: {test_data}")
    except Exception as e:
        print(f"⚠️  Test échoué: {e}")

if __name__ == "__main__":
    main()
