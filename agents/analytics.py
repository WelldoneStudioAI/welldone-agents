"""
agents/analytics.py — Agent Google Analytics 4 + Search Console.

Fusionne analytics.py + search_console.py + email_rapport.py de l'ancienne archi.
Utilise le Service Account Google (jamais d'expiration).
"""
import logging
from datetime import datetime, timedelta
from agents._base import BaseAgent
from core.auth import get_service_account_creds, get_google_service
from config import GA4_PROPERTY_ID, GSC_SITE_STUDIO, GSC_SITE_ARCHI

log = logging.getLogger(__name__)

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


class AnalyticsAgent(BaseAgent):
    name        = "analytics"
    description = "Rapports GA4 et Search Console pour Welldone Studio"
    schedules   = [{"cron": "0 13 * * 1", "command": "rapport"}]  # Lundi 8h00 Montreal

    @property
    def commands(self):
        return {
            "rapport":      self.rapport,
            "sources":      self.sources,
            "keywords":     self.keywords,
            "opportunities": self.opportunities,
        }

    # ── GA4 ───────────────────────────────────────────────────────────────────

    async def rapport(self, context: dict | None = None) -> str:
        """Rapport hebdomadaire complet: GA4 + GSC + email."""
        days = int((context or {}).get("days", 7))
        try:
            totals, sources_rows, pages_rows, organic, direct, start, end = self._ga4_summary(days)
            keywords, opps = self._gsc_keywords()
            analyse  = self._analyse(totals, organic, direct, opps)
            html     = self._build_html(totals, sources_rows, pages_rows, keywords, opps, analyse, start, end, days)
            self._send_email(html, start, end)
            log.info(f"analytics.rapport sent days={days}")
            return f"✅ Rapport Analytics envoyé ({start} → {end})\n📊 {totals['sessions']:,} sessions · {totals['users']:,} utilisateurs"
        except Exception as e:
            log.error(f"analytics.rapport error: {e}")
            return f"❌ Erreur rapport analytics: {e}"

    async def sources(self, context: dict | None = None) -> str:
        """Trafic par source."""
        days     = int((context or {}).get("days", 30))
        end      = datetime.today().strftime("%Y-%m-%d")
        start    = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy

        try:
            creds  = get_service_account_creds(GA4_SCOPES)
            client = BetaAnalyticsDataClient(credentials=creds)
            req    = RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
                date_ranges=[DateRange(start_date=start, end_date=end)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            )
            resp  = client.run_report(req)
            lines = [f"🔍 *Trafic par source ({days}j) :*\n"]
            for row in resp.rows:
                src  = row.dimension_values[0].value
                sess = row.metric_values[0].value
                usr  = row.metric_values[1].value
                lines.append(f"• {src}: {sess} sessions · {usr} users")
            return "\n".join(lines) if resp.rows else "Aucune donnée"
        except Exception as e:
            log.error(f"analytics.sources error: {e}")
            return f"❌ Erreur sources analytics: {e}"

    async def keywords(self, context: dict | None = None) -> str:
        """Top mots-clés Search Console."""
        site = (context or {}).get("site", "studio")
        site_url = GSC_SITE_ARCHI if site == "archi" else GSC_SITE_STUDIO
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=28)).strftime("%Y-%m-%d")

        try:
            from googleapiclient.discovery import build
            creds = get_service_account_creds(GSC_SCOPES)
            svc   = build("searchconsole", "v1", credentials=creds)
            rows  = svc.searchanalytics().query(
                siteUrl=site_url,
                body={"startDate": start, "endDate": end, "dimensions": ["query"],
                      "rowLimit": 10, "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}]},
            ).execute().get("rows", [])

            if not rows:
                return f"🔍 Aucun mot-clé GSC pour {site_url}"

            lines = [f"🔍 *Top mots-clés ({site}) — 28j :*\n"]
            for r in rows:
                kw   = r["keys"][0][:40]
                clics = int(r["clicks"])
                impr  = int(r["impressions"])
                pos   = f"{r['position']:.1f}"
                lines.append(f"• {kw}: {clics} clics · {impr} impr · pos {pos}")
            return "\n".join(lines)
        except Exception as e:
            log.error(f"analytics.keywords error: {e}")
            return f"❌ Erreur mots-clés GSC: {e}"

    async def opportunities(self, context: dict | None = None) -> str:
        """Mots-clés en pos 4-20 avec beaucoup d'impressions = opportunités SEO."""
        site = (context or {}).get("site", "studio")
        site_url = GSC_SITE_ARCHI if site == "archi" else GSC_SITE_STUDIO
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=28)).strftime("%Y-%m-%d")

        try:
            from googleapiclient.discovery import build
            creds = get_service_account_creds(GSC_SCOPES)
            svc   = build("searchconsole", "v1", credentials=creds)
            rows  = svc.searchanalytics().query(
                siteUrl=site_url,
                body={"startDate": start, "endDate": end, "dimensions": ["query"],
                      "rowLimit": 200, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
            ).execute().get("rows", [])

            opps = sorted(
                [r for r in rows if 4 <= r["position"] <= 20 and r["impressions"] >= 10],
                key=lambda x: x["impressions"], reverse=True
            )[:10]

            if not opps:
                return "🚀 Aucune opportunité SEO immédiate (pos 4-20, 10+ impressions)"

            lines = [f"🚀 *Opportunités SEO pos 4-20 ({site}) :*\n"]
            for r in opps:
                kw   = r["keys"][0][:40]
                impr = int(r["impressions"])
                pos  = f"{r['position']:.1f}"
                lines.append(f"• {kw}: {impr} impr · pos {pos}")
            return "\n".join(lines)
        except Exception as e:
            log.error(f"analytics.opportunities error: {e}")
            return f"❌ Erreur opportunités GSC: {e}"

    # ── Helpers privés ────────────────────────────────────────────────────────

    def _ga4_summary(self, days: int = 7):
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy

        creds  = get_service_account_creds(GA4_SCOPES)
        client = BetaAnalyticsDataClient(credentials=creds)
        end    = datetime.today().strftime("%Y-%m-%d")
        start  = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        start_prev = (datetime.today() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        end_prev   = (datetime.today() - timedelta(days=days + 1)).strftime("%Y-%m-%d")

        def _run(s, e, limit=10):
            r_sources = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers"),
                         Metric(name="screenPageViews"), Metric(name="bounceRate"),
                         Metric(name="averageSessionDuration")],
                date_ranges=[DateRange(start_date=s, end_date=e)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            ))
            r_pages = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="pagePath")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers"), Metric(name="screenPageViews")],
                date_ranges=[DateRange(start_date=s, end_date=e)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
                limit=limit,
            ))
            return r_sources, r_pages

        curr_s, curr_p = _run(start, end)
        prev_s, _      = _run(start_prev, end_prev)

        def totals(r):
            return {
                "sessions": sum(int(x.metric_values[0].value) for x in r.rows),
                "users":    sum(int(x.metric_values[1].value) for x in r.rows),
                "views":    sum(int(x.metric_values[2].value) for x in r.rows),
                "bounce":   sum(float(x.metric_values[3].value) for x in r.rows) / max(len(r.rows), 1),
                "duration": sum(float(x.metric_values[4].value) for x in r.rows) / max(len(r.rows), 1),
            }

        curr_t = totals(curr_s)
        prev_t = totals(prev_s)

        def pct(c, p):
            return ((c - p) / p * 100) if p else 0

        curr_t["sessions_delta"] = pct(curr_t["sessions"], prev_t["sessions"])
        curr_t["users_delta"]    = pct(curr_t["users"],    prev_t["users"])
        curr_t["views_delta"]    = pct(curr_t["views"],    prev_t["views"])

        organic = next((int(r.metric_values[0].value) for r in curr_s.rows if "Organic" in r.dimension_values[0].value), 0)
        direct  = next((int(r.metric_values[0].value) for r in curr_s.rows if "Direct"  in r.dimension_values[0].value), 0)

        return curr_t, curr_s.rows, curr_p.rows, organic, direct, start, end

    def _gsc_keywords(self):
        from googleapiclient.discovery import build
        creds = get_service_account_creds(GSC_SCOPES)
        svc   = build("searchconsole", "v1", credentials=creds)
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=28)).strftime("%Y-%m-%d")

        keywords = svc.searchanalytics().query(
            siteUrl=GSC_SITE_STUDIO,
            body={"startDate": start, "endDate": end, "dimensions": ["query"],
                  "rowLimit": 10, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        ).execute().get("rows", [])

        all_rows = svc.searchanalytics().query(
            siteUrl=GSC_SITE_STUDIO,
            body={"startDate": start, "endDate": end, "dimensions": ["query"],
                  "rowLimit": 200, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        ).execute().get("rows", [])

        opps = sorted(
            [r for r in all_rows if 4 <= r["position"] <= 20 and r["impressions"] >= 5],
            key=lambda x: x["impressions"], reverse=True
        )[:8]

        return keywords, opps

    def _analyse(self, totals, organic, direct, opps) -> list[str]:
        sessions       = totals["sessions"]
        sessions_delta = totals["sessions_delta"]
        organic_pct    = organic / max(sessions, 1) * 100
        direct_pct     = direct  / max(sessions, 1) * 100
        bounce_pct     = totals["bounce"] * 100
        duration_min   = totals["duration"] / 60

        lignes = []
        if sessions_delta >= 10:
            lignes.append(f"📈 <strong>Bonne semaine</strong> — trafic en hausse de <strong>{sessions_delta:+.0f}%</strong> vs semaine précédente.")
        elif sessions_delta <= -10:
            lignes.append(f"📉 <strong>Attention</strong> — trafic en baisse de <strong>{sessions_delta:.0f}%</strong> vs semaine précédente.")
        else:
            lignes.append(f"→ Trafic stable (<strong>{sessions_delta:+.0f}%</strong> vs semaine précédente).")

        if duration_min >= 3:
            lignes.append(f"✅ Visiteurs <strong>engagés</strong> — durée moy. <strong>{duration_min:.1f} min</strong>/session.")
        elif duration_min >= 1.5:
            lignes.append(f"→ Engagement modéré ({duration_min:.1f} min/session).")
        else:
            lignes.append(f"⚠️ Durée courte ({duration_min:.1f} min/session) — revoir l'accroche des pages.")

        if bounce_pct < 35:
            lignes.append(f"✅ Taux de rebond excellent ({bounce_pct:.0f}%).")
        elif bounce_pct < 55:
            lignes.append(f"→ Taux de rebond acceptable ({bounce_pct:.0f}%).")
        else:
            lignes.append(f"⚠️ Taux de rebond élevé ({bounce_pct:.0f}%).")

        if organic_pct < 15:
            lignes.append(f"⚠️ Seulement <strong>{organic_pct:.0f}% de trafic organique</strong>. SEO = levier principal inexploité.")
        elif organic_pct < 35:
            lignes.append(f"→ Trafic organique à <strong>{organic_pct:.0f}%</strong> — en progression.")
        else:
            lignes.append(f"✅ Bon trafic organique ({organic_pct:.0f}%).")

        if direct_pct > 55:
            lignes.append(f"✅ Fort trafic direct ({direct_pct:.0f}%) — excellente notoriété de marque.")

        if opps:
            lignes.append(f"🚀 <strong>{len(opps)} mots-clés en position 4-20</strong> pourraient passer en page 1.")
        else:
            lignes.append("→ Aucune opportunité SEO immédiate cette semaine.")

        return lignes

    def _build_html(self, totals, sources_rows, pages_rows, keywords, opps, analyse, start, end, days) -> str:
        today = datetime.now().strftime("%d %b %Y")

        def badge(val):
            if val > 5:  return f'<span style="color:#4ade80;font-size:11px"> ▲ {val:+.0f}%</span>'
            if val < -5: return f'<span style="color:#f87171;font-size:11px"> ▼ {val:.0f}%</span>'
            return f'<span style="color:#888;font-size:11px"> → {val:+.0f}%</span>'

        analyse_html = "".join(f"<p style='margin:8px 0;line-height:1.7'>{l}</p>" for l in analyse)
        ts  = "width:100%;border-collapse:collapse;margin:12px 0;font-size:13px"
        th  = "background:#1e1e1e;color:#aaa;padding:8px 10px;text-align:left;border-bottom:1px solid #333"
        td  = "padding:7px 10px;border-bottom:1px solid #1a1a1a;color:#ddd"
        tdr = td + ";text-align:right"

        src_rows = "".join(
            f'<tr><td style="{td}">{r.dimension_values[0].value}</td>'
            f'<td style="{tdr}">{int(r.metric_values[0].value):,}</td>'
            f'<td style="{tdr}">{int(r.metric_values[1].value):,}</td></tr>'
            for r in sources_rows
        )
        pg_rows = "".join(
            f'<tr><td style="{td};font-family:monospace;font-size:12px">{r.dimension_values[0].value[:52]}</td>'
            f'<td style="{tdr}">{int(r.metric_values[0].value):,}</td>'
            f'<td style="{tdr}">{int(r.metric_values[1].value):,}</td></tr>'
            for r in pages_rows
        )
        kw_rows = "".join(
            f'<tr><td style="{td}">{r["keys"][0][:45]}</td>'
            f'<td style="{tdr}">{int(r["impressions"]):,}</td>'
            f'<td style="{tdr}">{int(r["clicks"])}</td>'
            f'<td style="{tdr}">{r["position"]:.0f}</td></tr>'
            for r in keywords
        )
        opp_rows = "".join(
            f'<tr><td style="{td}">{r["keys"][0][:45]}</td>'
            f'<td style="{tdr}">{int(r["impressions"]):,}</td>'
            f'<td style="{tdr}">{r["position"]:.0f}</td></tr>'
            for r in opps
        )

        return f"""<html><body style="margin:0;padding:0;background:#0d0d0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e0e0e0">
<div style="max-width:680px;margin:0 auto;padding:32px 24px">
  <div style="border-bottom:1px solid #222;padding-bottom:20px;margin-bottom:28px">
    <p style="margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px">Welldone Studio · Rapport automatique</p>
    <h1 style="margin:6px 0 4px;font-size:22px;font-weight:600;color:#fff">Semaine du {today}</h1>
    <p style="margin:0;color:#666;font-size:12px">{start} → {end} · {days} jours</p>
  </div>
  <div style="display:flex;gap:12px;margin-bottom:28px">
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['sessions']:,}{badge(totals['sessions_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Sessions</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['users']:,}{badge(totals['users_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Utilisateurs</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['views']:,}{badge(totals['views_delta'])}</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Pages vues</div>
    </div>
    <div style="flex:1;background:#111;border:1px solid #222;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:26px;font-weight:700;color:#fff">{totals['duration']/60:.1f} min</div>
      <div style="font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:1px">Durée moy.</div>
    </div>
  </div>
  <div style="background:#111;border:1px solid #1e3a1e;border-left:3px solid #4ade80;border-radius:8px;padding:20px;margin-bottom:28px">
    <p style="margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#4ade80;font-weight:600">Analyse de la semaine</p>
    {analyse_html}
  </div>
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:0 0 8px">Sources de trafic</h3>
  <table style="{ts}"><tr><th style="{th}">Source</th><th style="{th};text-align:right">Sessions</th><th style="{th};text-align:right">Utilisateurs</th></tr>{src_rows}</table>
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">Top pages</h3>
  <table style="{ts}"><tr><th style="{th}">Page</th><th style="{th};text-align:right">Sessions</th><th style="{th};text-align:right">Utilisateurs</th></tr>{pg_rows}</table>
  <h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">Mots-clés Google (28 jours)</h3>
  {'<table style="' + ts + '"><tr><th style="' + th + '">Mot-clé</th><th style="' + th + ';text-align:right">Impr.</th><th style="' + th + ';text-align:right">Clics</th><th style="' + th + ';text-align:right">Pos.</th></tr>' + kw_rows + '</table>' if kw_rows else '<p style="color:#666;font-size:13px">Aucune donnée Search Console.</p>'}
  {('<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 8px">🚀 Opportunités SEO (pos. 4-20)</h3><table style="' + ts + '"><tr><th style="' + th + '">Mot-clé</th><th style="' + th + ';text-align:right">Impressions</th><th style="' + th + ';text-align:right">Position</th></tr>' + opp_rows + '</table>') if opp_rows else ''}
  <div style="border-top:1px solid #1a1a1a;margin-top:32px;padding-top:16px;text-align:center">
    <p style="margin:0;color:#444;font-size:11px">Welldone Studio AI System · Rapport automatique chaque lundi 8h</p>
  </div>
</div></body></html>"""

    def _send_email(self, html: str, start: str, end: str):
        import base64
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from googleapiclient.discovery import build
        from config import GMAIL_RECIPIENT

        today   = datetime.now().strftime("%d %b %Y")
        subject = f"📊 Welldone — Rapport semaine du {today}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_RECIPIENT
        msg["To"]      = GMAIL_RECIPIENT
        msg.attach(MIMEText(html, "html"))

        from core.auth import get_oauth_creds
        creds   = get_oauth_creds()
        service = build("gmail", "v1", credentials=creds)
        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


agent = AnalyticsAgent()
