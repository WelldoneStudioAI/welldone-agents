#!/usr/bin/env python3
"""
Google Search Console — Welldone Studio
Sites: awelldone.com (studio) / welldone.archi (archi)
"""

import os
import sys
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
CREDENTIALS_FILE = os.path.expanduser("~/.config/gws/client_secret.json")
TOKEN_FILE = os.path.expanduser("~/.config/gws/searchconsole_token.json")

SITES = {
    "studio": "sc-domain:awelldone.com",
    "archi": "sc-domain:welldone.archi",
}


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def list_sites():
    creds = get_credentials()
    service = build("searchconsole", "v1", credentials=creds)
    result = service.sites().list().execute()
    sites = result.get("siteEntry", [])
    print("\n🌐 Sites Search Console\n")
    for s in sites:
        print(f"  {s['permissionLevel']:15} {s['siteUrl']}")
    return [s["siteUrl"] for s in sites]


def top_keywords(site_key="studio", start_date="2026-02-13", end_date="2026-03-15", limit=25):
    site_url = SITES.get(site_key, site_key)
    creds = get_credentials()
    service = build("searchconsole", "v1", credentials=creds)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query"],
        "rowLimit": limit,
        "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
    }

    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = response.get("rows", [])

    print(f"\n🔍 Top mots-clés — {site_url} ({start_date} → {end_date})\n")
    print(f"{'Mot-clé':<45} {'Clics':>7} {'Impr.':>8} {'CTR':>7} {'Pos.':>6}")
    print("-" * 78)
    for row in rows:
        keyword = row["keys"][0][:43]
        clicks = int(row["clicks"])
        impressions = int(row["impressions"])
        ctr = f"{row['ctr']*100:.1f}%"
        position = f"{row['position']:.1f}"
        print(f"{keyword:<45} {clicks:>7} {impressions:>8} {ctr:>7} {position:>6}")


def top_pages(site_key="studio", start_date="2026-02-13", end_date="2026-03-15", limit=20):
    site_url = SITES.get(site_key, site_key)
    creds = get_credentials()
    service = build("searchconsole", "v1", credentials=creds)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["page"],
        "rowLimit": limit,
        "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
    }

    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = response.get("rows", [])

    print(f"\n📄 Top pages — {site_url} ({start_date} → {end_date})\n")
    print(f"{'Page':<55} {'Clics':>7} {'Impr.':>8} {'CTR':>7} {'Pos.':>6}")
    print("-" * 85)
    for row in rows:
        page = row["keys"][0].replace(site_url, "/")[:53]
        clicks = int(row["clicks"])
        impressions = int(row["impressions"])
        ctr = f"{row['ctr']*100:.1f}%"
        position = f"{row['position']:.1f}"
        print(f"{page:<55} {clicks:>7} {impressions:>8} {ctr:>7} {position:>6}")


def opportunities(site_key="studio", start_date="2026-02-13", end_date="2026-03-15"):
    """Mots-clés en position 4-20 avec beaucoup d'impressions = opportunités SEO"""
    site_url = SITES.get(site_key, site_key)
    creds = get_credentials()
    service = build("searchconsole", "v1", credentials=creds)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query"],
        "rowLimit": 200,
        "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}],
    }

    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = response.get("rows", [])

    # Filtrer pos 4-20 avec 10+ impressions
    opps = [r for r in rows if 4 <= r["position"] <= 20 and r["impressions"] >= 10]
    opps.sort(key=lambda x: x["impressions"], reverse=True)

    print(f"\n🚀 Opportunités SEO — {site_url} (pos. 4-20, 10+ impressions)\n")
    print(f"{'Mot-clé':<45} {'Impr.':>8} {'Clics':>7} {'CTR':>7} {'Pos.':>6}")
    print("-" * 78)
    for row in opps[:25]:
        keyword = row["keys"][0][:43]
        impressions = int(row["impressions"])
        clicks = int(row["clicks"])
        ctr = f"{row['ctr']*100:.1f}%"
        position = f"{row['position']:.1f}"
        print(f"{keyword:<45} {impressions:>8} {clicks:>7} {ctr:>7} {position:>6}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "keywords"
    site = sys.argv[2] if len(sys.argv) > 2 else "studio"
    start = sys.argv[3] if len(sys.argv) > 3 else "2026-02-13"
    end = sys.argv[4] if len(sys.argv) > 4 else "2026-03-15"

    if mode == "sites":
        list_sites()
    elif mode == "pages":
        top_pages(site, start, end)
    elif mode == "opportunities":
        opportunities(site, start, end)
    else:
        top_keywords(site, start, end)
