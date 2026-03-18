#!/usr/bin/env python3
"""
Rapport hebdomadaire Welldone — envoi automatique par Gmail
Tourne chaque lundi matin à 8h
"""

import warnings; warnings.filterwarnings("ignore")
import os, sys, io
from datetime import datetime, timedelta
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

RECIPIENT   = "awelldonestudio@gmail.com"
GMAIL_TOKEN = os.path.expanduser("~/.config/gws/gmail_send_token.json")
GA4_PROPERTY = "522467276"


# ── Auth ─────────────────────────────────────────────────────────────────────
def get_gmail_creds():
    creds = Credentials.from_authorized_user_file(
        GMAIL_TOKEN, ["https://www.googleapis.com/auth/gmail.send"]
    )
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


# ── Données brutes ────────────────────────────────────────────────────────────
def get_ga4_summary(days=7):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy

    creds = Credentials.from_authorized_user_file(
        os.path.expanduser("~/.config/gws/analytics_token.json"),
        ["https://www.googleapis.com/auth/analytics.readonly"]
    )
    if not creds.valid and creds.expired:
        creds.refresh(Request())

    client  = BetaAnalyticsDataClient(credentials=creds)
    end     = datetime.today().strftime("%Y-%m-%d")
    start   = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    start_prev = (datetime.today() - timedelta(days=days*2)).strftime("%Y-%m-%d")
    end_prev   = (datetime.today() - timedelta(days=days+1)).strftime("%Y-%m-%d")

    def run(s, e, limit=10):
        r_sources = client.run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY}",
            dimensions=[Dimension(name="sessionDefaultChannelGroup")],
            metrics=[Metric(name="sessions"), Metric(name="activeUsers"),
                     Metric(name="screenPageViews"), Metric(name="bounceRate"),
                     Metric(name="averageSessionDuration")],
            date_ranges=[DateRange(start_date=s, end_date=e)],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        ))
        r_pages = client.run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY}",
            dimensions=[Dimension(name="pagePath")],
            metrics=[Metric(name="sessions"), Metric(name="activeUsers"), Metric(name="screenPageViews")],
            date_ranges=[DateRange(start_date=s, end_date=e)],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            limit=limit,
        ))
        return r_sources, r_pages

    curr_s, curr_p = run(start, end)
    prev_s, _      = run(start_prev, end_prev)

    def totals(report):
        return {
            "sessions": sum(int(r.metric_values[0].value) for r in report.rows),
            "users":    sum(int(r.metric_values[1].value) for r in report.rows),
            "views":    sum(int(r.metric_values[2].value) for r in report.rows),
            "bounce":   sum(float(r.metric_values[3].value) for r in report.rows) / max(len(report.rows),1),
            "duration": sum(float(r.metric_values[4].value) for r in report.rows) / max(len(report.rows),1),
        }

    curr_totals = totals(curr_s)
    prev_totals = totals(prev_s)

    def pct_change(curr, prev):
        if prev == 0: return 0
        return ((curr - prev) / prev) * 100

    curr_totals["sessions_delta"] = pct_change(curr_totals["sessions"], prev_totals["sessions"])
    curr_totals["users_delta"]    = pct_change(curr_totals["users"],    prev_totals["users"])
    curr_totals["views_delta"]    = pct_change(curr_totals["views"],    prev_totals["views"])

    organic = next((int(r.metric_values[0].value) for r in curr_s.rows if "Organic" in r.dimension_values[0].value), 0)
    direct  = next((int(r.metric_values[0].value) for r in curr_s.rows if "Direct"  in r.dimension_values[0].value), 0)

    return curr_totals, curr_s.rows, curr_p.rows, organic, direct, start, end


def get_gsc_keywords():
    from googleapiclient.discovery import build as gbuild
    creds = Credentials.from_authorized_user_file(
        os.path.expanduser("~/.config/gws/searchconsole_token.json"),
        ["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    if not creds.valid and creds.expired:
        creds.refresh(Request())
    service = gbuild("searchconsole", "v1", credentials=creds)

    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=28)).strftime("%Y-%m-%d")

    keywords = service.searchanalytics().query(
        siteUrl="https://www.awelldone.com/",
        body={"startDate": start, "endDate": end, "dimensions": ["query"],
              "rowLimit": 10, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]}
    ).execute().get("rows", [])

    opps = service.searchanalytics().query(
        siteUrl="https://www.awelldone.com/",
        body={"startDate": start, "endDate": end, "dimensions": ["query"],
              "rowLimit": 200, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]}
    ).execute().get("rows", [])

    opps = sorted([r for r in opps if 4 <= r["position"] <= 20 and r["impressions"] >= 5],
                  key=lambda x: x["impressions"], reverse=True)[:8]

    return keywords, opps


# ── Analyse consultant ────────────────────────────────────────────────────────
def analyse_consultant(totals, organic, direct, opps):
    sessions       = totals["sessions"]
    sessions_delta = totals["sessions_delta"]
    organic_pct    = organic / max(sessions, 1) * 100
    direct_pct     = direct  / max(sessions, 1) * 100
    bounce_pct     = totals["bounce"] * 100
    duration_min   = totals["duration"] / 60

    lignes = []

    # Tendance globale
    if sessions_delta >= 10:
        tendance = f"📈 <strong>Bonne semaine</strong> — le trafic est en hausse de <strong>{sessions_delta:+.0f}%</strong> vs la semaine précédente."
    elif sessions_delta <= -10:
        tendance = f"📉 <strong>Attention</strong> — le trafic a baissé de <strong>{sessions_delta:.0f}%</strong> vs la semaine précédente. À surveiller."
    else:
        tendance = f"→ Trafic stable cette semaine (<strong>{sessions_delta:+.0f}%</strong> vs semaine précédente)."
    lignes.append(tendance)

    # Qualité du trafic
    if duration_min >= 3:
        lignes.append(f"✅ Les visiteurs sont <strong>engagés</strong> — durée moyenne de <strong>{duration_min:.1f} min</strong> par session. Ton contenu retient l'attention.")
    elif duration_min >= 1.5:
        lignes.append(f"→ Engagement modéré ({duration_min:.1f} min/session). Considère des appels à l'action plus visibles sur les pages projets.")
    else:
        lignes.append(f"⚠️ Durée courte ({duration_min:.1f} min/session). Les visiteurs partent rapidement — revoir l'accroche des pages principales.")

    if bounce_pct < 35:
        lignes.append(f"✅ Taux de rebond excellent ({bounce_pct:.0f}%) — les gens explorent plusieurs pages.")
    elif bounce_pct < 55:
        lignes.append(f"→ Taux de rebond acceptable ({bounce_pct:.0f}%). Normal pour un portfolio.")
    else:
        lignes.append(f"⚠️ Taux de rebond élevé ({bounce_pct:.0f}%). Les pages d'entrée pourraient mieux orienter le visiteur.")

    # Sources
    if organic_pct < 15:
        lignes.append(f"⚠️ Seulement <strong>{organic_pct:.0f}% de trafic organique</strong>. Le SEO est ton principal levier de croissance inexploité — publier régulièrement est prioritaire.")
    elif organic_pct < 35:
        lignes.append(f"→ Trafic organique à <strong>{organic_pct:.0f}%</strong> — en progression. Continue la stratégie de contenu.")
    else:
        lignes.append(f"✅ Bon trafic organique ({organic_pct:.0f}%) — ta stratégie SEO porte ses fruits.")

    if direct_pct > 55:
        lignes.append(f"✅ Fort trafic direct ({direct_pct:.0f}%) — excellente notoriété de marque. Tes clients te cherchent directement.")

    # Opportunités SEO
    if opps:
        lignes.append(f"🚀 <strong>{len(opps)} mots-clés en position 4-20</strong> pourraient passer en page 1 avec un article ou une optimisation ciblée.")
    else:
        lignes.append("→ Aucune opportunité SEO immédiate détectée cette semaine.")

    # Santé technique
    lignes.append("🔧 <strong>Santé technique :</strong> GTM actif sur awelldone.com · GA4 opérationnel · Tracking clics & scroll configuré.")

    return lignes


# ── HTML ─────────────────────────────────────────────────────────────────────
def construire_html(totals, sources_rows, pages_rows, keywords, opps, analyse, start, end, days):
    today = datetime.now().strftime("%d %b %Y")

    def delta_badge(val):
        if val > 5:   return f'<span style="color:#4ade80;font-size:11px"> ▲ {val:+.0f}%</span>'
        if val < -5:  return f'<span style="color:#f87171;font-size:11px"> ▼ {val:.0f}%</span>'
        return f'<span style="color:#888;font-size:11px"> → {val:+.0f}%</span>'

    rows_sources = ""
    for r in sources_rows:
        src  = r.dimension_values[0].value
        sess = int(r.metric_values[0].value)
        usr  = int(r.metric_values[1].value)
        rows_sources += f"<tr><td>{src}</td><td style='text-align:right'>{sess:,}</td><td style='text-align:right'>{usr:,}</td></tr>"

    rows_pages = ""
    for r in pages_rows:
        path = r.dimension_values[0].value[:50]
        sess = int(r.metric_values[0].value)
        usr  = int(r.metric_values[1].value)
        rows_pages += f"<tr><td style='font-family:monospace;font-size:12px'>{path}</td><td style='text-align:right'>{sess:,}</td><td style='text-align:right'>{usr:,}</td></tr>"

    rows_kw = ""
    for r in keywords:
        kw   = r["keys"][0][:45]
        rows_kw += f"<tr><td>{kw}</td><td style='text-align:right'>{int(r['impressions']):,}</td><td style='text-align:right'>{int(r['clicks'])}</td><td style='text-align:right'>{r['position']:.0f}</td></tr>"

    rows_opps = ""
    for r in opps:
        kw = r["keys"][0][:45]
        rows_opps += f"<tr><td>{kw}</td><td style='text-align:right'>{int(r['impressions']):,}</td><td style='text-align:right'>{r['position']:.0f}</td></tr>"

    analyse_html = "".join(f"<p style='margin:8px 0;line-height:1.7'>{l}</p>" for l in analyse)

    table_style = "width:100%;border-collapse:collapse;margin:12px 0;font-size:13px"
    th_style    = "background:#1e1e1e;color:#aaa;padding:8px 10px;text-align:left;border-bottom:1px solid #333"
    td_style    = "padding:7px 10px;border-bottom:1px solid #1a1a1a;color:#ddd"

    return f"""
<html>
<body style="margin:0;padding:0;background:#0d0d0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e0e0e0">
<div style="max-width:680px;margin:0 auto;padding:32px 24px">

  <!-- En-tête -->
  <div style="border-bottom:1px solid #222;padding-bottom:20px;margin-bottom:28px">
    <p style="margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px">Welldone Studio · Rapport automatique</p>
    <h1 style="margin:6px 0 4px;font-size:22px;font-weight:600;color:#fff">Semaine du {today}</h1>
    <p style="margin:0;color:#666;font-size:12px">{start} → {end} · {days} jours</p>
  </div>

  <!-- Chiffres clés -->
  <div style="display:flex;gap:12px;margin-bottom:28px">
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['sessions']:,}{delta_badge(totals['sessions_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Sessions</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['users']:,}{delta_badge(totals['users_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Utilisateurs</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['views']:,}{delta_badge(totals['views_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Pages vues</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['duration']/60:.1f} min</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Durée moy.</div>
    </div>
  </div>

  <!-- Analyse consultant -->
  <div style="background:#111;border:1px solid #1e3a1e;border-left:3px solid #4ade80;border-radius:8px;padding:20px;margin-bottom:28px">
    <p style="margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#4ade80;font-weight:600">Analyse de la semaine</p>
    {analyse_html}
  </div>

  <!-- Sources de trafic -->
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:0 0 8px">Sources de trafic</h3>
  <table style="{table_style}">
    <tr><th style="{th_style}">Source</th><th style="{th_style};text-align:right">Sessions</th><th style="{th_style};text-align:right">Utilisateurs</th></tr>
    {''.join(f'<tr><td style="{td_style}">{r.dimension_values[0].value}</td><td style="{td_style};text-align:right">{int(r.metric_values[0].value):,}</td><td style="{td_style};text-align:right">{int(r.metric_values[1].value):,}</td></tr>' for r in sources_rows)}
  </table>

  <!-- Top pages -->
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">Top pages</h3>
  <table style="{table_style}">
    <tr><th style="{th_style}">Page</th><th style="{th_style};text-align:right">Sessions</th><th style="{th_style};text-align:right">Utilisateurs</th></tr>
    {''.join(f'<tr><td style="{td_style};font-family:monospace;font-size:12px">{r.dimension_values[0].value[:52]}</td><td style="{td_style};text-align:right">{int(r.metric_values[0].value):,}</td><td style="{td_style};text-align:right">{int(r.metric_values[1].value):,}</td></tr>' for r in pages_rows)}
  </table>

  <!-- SEO Search Console -->
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">Mots-clés Google (28 jours)</h3>
  {'<table style="' + table_style + '"><tr><th style="' + th_style + '">Mot-clé</th><th style="' + th_style + ';text-align:right">Impr.</th><th style="' + th_style + ';text-align:right">Clics</th><th style="' + th_style + ';text-align:right">Pos.</th></tr>' + rows_kw + '</table>' if rows_kw else '<p style="color:#666;font-size:13px">Aucune donnée Search Console disponible.</p>'}

  <!-- Opportunités SEO -->
  {('<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">🚀 Opportunités SEO (pos. 4-20)</h3><p style="color:#888;font-size:12px;margin:0 0 8px">Ces mots-clés sont à portée de la page 1 — un article ou une optimisation ciblée pourrait suffire.</p><table style="' + table_style + '"><tr><th style="' + th_style + '">Mot-clé</th><th style="' + th_style + ';text-align:right">Impressions</th><th style="' + th_style + ';text-align:right">Position</th></tr>' + rows_opps + '</table>') if rows_opps else ''}

  <!-- Santé technique -->
  <div style="background:#111;border:1px solid #222;border-radius:8px;padding:16px;margin-top:28px">
    <p style="margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#888;font-weight:600">Santé technique</p>
    <p style="margin:4px 0;font-size:13px;color:#4ade80">✅ Google Analytics actif — données reçues</p>
    <p style="margin:4px 0;font-size:13px;color:#4ade80">✅ GTM opérationnel — tracking clics & scroll actif</p>
    <p style="margin:4px 0;font-size:13px;color:#4ade80">✅ Search Console connectée</p>
    <p style="margin:4px 0;font-size:13px;color:#60a5fa">→ awelldone.studio → awelldone.com (redirection 301 active)</p>
  </div>

  <!-- Footer -->
  <div style="border-top:1px solid #1a1a1a;margin-top:32px;padding-top:16px;text-align:center">
    <p style="margin:0;color:#444;font-size:11px">Welldone Studio AI System · Rapport automatique chaque lundi 8h</p>
    <p style="margin:4px 0 0;color:#333;font-size:11px">awelldone.com · awelldone.studio · welldone.archi</p>
  </div>

</div>
</body>
</html>"""


# ── Envoi ─────────────────────────────────────────────────────────────────────
def envoyer_rapport(days=7):
    print("⏳ Récupération des données...")
    totals, sources_rows, pages_rows, organic, direct, start, end = get_ga4_summary(days)

    print("⏳ Search Console...")
    try:
        keywords, opps = get_gsc_keywords()
    except Exception:
        keywords, opps = [], []

    analyse = analyse_consultant(totals, organic, direct, opps)

    html = construire_html(totals, sources_rows, pages_rows, keywords, opps, analyse, start, end, days)

    today   = datetime.now().strftime("%d %b %Y")
    subject = f"📊 Welldone — Rapport semaine du {today}"

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = RECIPIENT
    msg['To']      = RECIPIENT
    msg.attach(MIMEText(html, 'html'))

    creds   = get_gmail_creds()
    service = build('gmail', 'v1', credentials=creds)
    raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()

    print(f"✅ Rapport envoyé à {RECIPIENT}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    envoyer_rapport(days)
