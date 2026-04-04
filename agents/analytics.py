"""
agents/analytics.py — Agent Google Analytics 4 + Search Console.

Rapport hebdomadaire complet :
  - GA4 : sessions, utilisateurs, pages vues, durée, rebond
  - GA4 : trafic par device (mobile/desktop/tablet)
  - GA4 : géographie (top pays + villes)
  - GA4 : nouveaux vs visiteurs de retour
  - GA4 : performance des articles de blog (/journal/)
  - GA4 : pages qui ont PERDU du trafic cette semaine
  - GA4 : événements CTA (clics boutons, formulaires)
  - GSC Studio : mots-clés + CTR + tendance semaine sur semaine
  - GSC Studio : opportunités pos. 4-20
  - GSC Archi : mots-clés (site archi séparé)
  - Synthèse : 3 actions prioritaires générées par Claude
"""
import logging
from datetime import datetime, timedelta
from agents._base import BaseAgent
from core.auth import get_service_account_creds
from config import GA4_PROPERTY_ID, GSC_SITE_STUDIO, GSC_SITE_ARCHI

log = logging.getLogger(__name__)

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Événements GA4 "bruit de fond" à masquer dans les rapports de conversion
_NOISE_EVENTS = {
    "page_view", "session_start", "first_visit", "user_engagement",
    "scroll", "click",
}


class AnalyticsAgent(BaseAgent):
    name        = "analytics"
    description = "Rapports GA4 + Search Console — Studio & Archi — complet avec tendances, géo, mobile, blog"
    schedules   = [{"cron": "0 13 * * 1", "command": "rapport"}]  # Lundi 8h00 Montréal

    @property
    def commands(self):
        return {
            "rapport":       self.rapport,
            "sources":       self.sources,
            "keywords":      self.keywords,
            "opportunities": self.opportunities,
            "conversions":   self.conversions,
        }

    # ── Commande principale ───────────────────────────────────────────────────

    async def rapport(self, context: dict | None = None) -> str:
        """Rapport hebdomadaire complet — toutes les sections."""
        days = int((context or {}).get("days", 7))
        try:
            # ── GA4 core
            totals, sources_rows, pages_rows, organic, direct, start, end = self._ga4_summary(days)
            events   = self._ga4_events(days)

            # ── GA4 enrichi (chaque section échoue silencieusement si API inaccessible)
            device_split  = self._safe(self._ga4_device_split, days)
            geo           = self._safe(self._ga4_geo, days)
            new_ret       = self._safe(self._ga4_new_vs_returning, days)
            blog_perf     = self._safe(self._ga4_blog_performance, days)
            losing_pages  = self._safe(self._ga4_losing_pages, days)

            # ── GSC Studio (mots-clés + CTR + tendance)
            keywords, kw_trend, opps = self._gsc_keywords_full()

            # ── GSC Archi
            archi_kw = self._safe(self._gsc_archi)

            # ── Analyse textuelle
            analyse  = self._analyse(totals, organic, direct, opps, events, device_split, new_ret)

            # ── Actions prioritaires (Claude synthétise)
            actions  = await self._synthesize_actions(
                totals, organic, direct, opps, events, device_split,
                geo, new_ret, blog_perf, losing_pages, kw_trend
            )

            # ── HTML + envoi email
            html = self._build_html(
                totals, sources_rows, pages_rows, keywords, kw_trend,
                opps, archi_kw, analyse, actions,
                start, end, days, events,
                device_split, geo, new_ret, blog_perf, losing_pages,
            )
            self._send_email(html, start, end)
            log.info(f"analytics.rapport sent days={days}")

            # ── Pipeline Notion (trace + contenu textuel) ──────────────────────
            notion_url = None
            try:
                from core.notion_delivery import pipeline_create as _pipeline_create
                _content = (
                    f"## Analyse — {start} → {end}\n\n"
                    + "\n".join(analyse)
                    + "\n\n## Actions prioritaires\n\n"
                    + "\n".join(actions)
                    + (
                        f"\n\n## Opportunités GSC (pos 4-20)\n\n"
                        + "\n".join(f"• {o}" for o in opps[:15])
                        if opps else ""
                    )
                )
                notion_url = await _pipeline_create(
                    title=f"Rapport Analytics — {start} → {end}",
                    agent="analytics",
                    type_="rapport",
                    content=_content,
                    status="Prêt révision",
                )
            except Exception as _ne:
                log.warning(f"analytics.rapport: notion pipeline skip ({_ne})")

            _notion_line = f"\n📋 [Rapport dans Notion]({notion_url})" if notion_url else ""
            return (
                f"✅ Rapport Analytics envoyé ({start} → {end})\n"
                f"📊 {totals['sessions']:,} sessions · {totals['users']:,} utilisateurs"
                + _notion_line
            )
        except Exception as e:
            log.error(f"analytics.rapport error: {e}", exc_info=True)
            return f"❌ Erreur rapport analytics: {e}"

    # ── Commandes individuelles ───────────────────────────────────────────────

    async def sources(self, context: dict | None = None) -> str:
        days = int((context or {}).get("days", 30))
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy
        try:
            creds  = get_service_account_creds(GA4_SCOPES)
            client = BetaAnalyticsDataClient(credentials=creds)
            resp   = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
                date_ranges=[DateRange(start_date=start, end_date=end)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            ))
            lines = [f"🔍 *Trafic par source ({days}j) :*\n"]
            for row in resp.rows:
                lines.append(f"• {row.dimension_values[0].value}: {row.metric_values[0].value} sessions")
            return "\n".join(lines) if resp.rows else "Aucune donnée"
        except Exception as e:
            return f"❌ Erreur sources analytics: {e}"

    async def keywords(self, context: dict | None = None) -> str:
        site     = (context or {}).get("site", "studio")
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
                      "rowLimit": 10, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
            ).execute().get("rows", [])
            if not rows:
                return f"🔍 Aucun mot-clé GSC pour {site_url}"
            lines = [f"🔍 *Top mots-clés ({site}) — 28j :*\n"]
            for r in rows:
                ctr = f"{r['ctr']*100:.1f}%"
                lines.append(
                    f"• {r['keys'][0][:40]}: {int(r['clicks'])} clics · "
                    f"{int(r['impressions'])} impr · pos {r['position']:.1f} · CTR {ctr}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Erreur mots-clés GSC: {e}"

    async def opportunities(self, context: dict | None = None) -> str:
        site     = (context or {}).get("site", "studio")
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
                      "rowLimit": 200},
            ).execute().get("rows", [])
            opps = sorted(
                [r for r in rows if 4 <= r["position"] <= 20 and r["impressions"] >= 5],
                key=lambda x: x["impressions"], reverse=True
            )[:10]
            if not opps:
                return "🚀 Aucune opportunité SEO immédiate (pos 4-20)"
            lines = [f"🚀 *Opportunités SEO pos 4-20 ({site}) :*\n"]
            for r in opps:
                ctr = f"{r['ctr']*100:.1f}%"
                lines.append(f"• {r['keys'][0][:40]}: {int(r['impressions'])} impr · pos {r['position']:.1f} · CTR {ctr}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Erreur opportunités GSC: {e}"

    async def conversions(self, context: dict | None = None) -> str:
        days = int((context or {}).get("days", 30))
        try:
            rows = self._ga4_events(days)
            if not rows:
                return f"📊 Aucun événement de conversion dans GA4 ({days}j).\n_Vérifie que Google Tag est configuré._"
            lines = [f"🎯 *Conversions & CTA ({days}j) :*\n"]
            for name, count in rows:
                lines.append(f"• `{name}` — {count:,} fois")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ Erreur conversions: {e}"

    # ── Helpers GA4 ───────────────────────────────────────────────────────────

    def _safe(self, fn, *args):
        """Appelle fn(*args), retourne None si exception — rapport jamais bloqué."""
        try:
            return fn(*args)
        except Exception as e:
            log.warning(f"analytics._safe({fn.__name__}): {e}")
            return None

    def _ga4_client(self):
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        return BetaAnalyticsDataClient(credentials=get_service_account_creds(GA4_SCOPES))

    def _date_range(self, days: int):
        from google.analytics.data_v1beta.types import DateRange
        end   = datetime.today()
        start = end - timedelta(days=days)
        return (
            DateRange(start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d")),
            DateRange(
                start_date=(start - timedelta(days=days)).strftime("%Y-%m-%d"),
                end_date=(end   - timedelta(days=days)).strftime("%Y-%m-%d"),
            ),
        )

    def _ga4_events(self, days: int = 30) -> list[tuple[str, int]]:
        from google.analytics.data_v1beta.types import RunReportRequest, Dimension, Metric, OrderBy
        dr, _ = self._date_range(days)
        resp  = self._ga4_client().run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
            date_ranges=[dr],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
        ))
        return [
            (row.dimension_values[0].value, int(row.metric_values[0].value))
            for row in resp.rows
            if row.dimension_values[0].value not in _NOISE_EVENTS
        ]

    def _ga4_summary(self, days: int = 7):
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy

        client = self._ga4_client()
        end    = datetime.today()
        start  = end - timedelta(days=days)
        prev_e = start - timedelta(days=1)
        prev_s = prev_e - timedelta(days=days)

        def fmt(d): return d.strftime("%Y-%m-%d")

        def _run(s, e, limit=10):
            r_src = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers"),
                         Metric(name="screenPageViews"), Metric(name="bounceRate"),
                         Metric(name="averageSessionDuration")],
                date_ranges=[DateRange(start_date=fmt(s), end_date=fmt(e))],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            ))
            r_pg = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="pagePath")],
                metrics=[Metric(name="sessions"), Metric(name="activeUsers"), Metric(name="screenPageViews")],
                date_ranges=[DateRange(start_date=fmt(s), end_date=fmt(e))],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
                limit=limit,
            ))
            return r_src, r_pg

        curr_s, curr_p = _run(start, end)
        prev_s_r, _    = _run(prev_s, prev_e)

        def totals(r):
            n = max(len(r.rows), 1)
            return {
                "sessions": sum(int(x.metric_values[0].value) for x in r.rows),
                "users":    sum(int(x.metric_values[1].value) for x in r.rows),
                "views":    sum(int(x.metric_values[2].value) for x in r.rows),
                "bounce":   sum(float(x.metric_values[3].value) for x in r.rows) / n,
                "duration": sum(float(x.metric_values[4].value) for x in r.rows) / n,
            }

        ct = totals(curr_s)
        pt = totals(prev_s_r)

        def pct(c, p): return ((c - p) / p * 100) if p else 0

        ct["sessions_delta"] = pct(ct["sessions"], pt["sessions"])
        ct["users_delta"]    = pct(ct["users"],    pt["users"])
        ct["views_delta"]    = pct(ct["views"],    pt["views"])
        ct["bounce_pct"]     = ct["bounce"] * 100

        organic = next((int(r.metric_values[0].value) for r in curr_s.rows if "Organic" in r.dimension_values[0].value), 0)
        direct  = next((int(r.metric_values[0].value) for r in curr_s.rows if "Direct"  in r.dimension_values[0].value), 0)

        return ct, curr_s.rows, curr_p.rows, organic, direct, fmt(start), fmt(end)

    def _ga4_device_split(self, days: int = 7) -> dict:
        """Sessions par device: desktop / mobile / tablet."""
        from google.analytics.data_v1beta.types import RunReportRequest, Dimension, Metric
        dr, _ = self._date_range(days)
        resp  = self._ga4_client().run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name="deviceCategory")],
            metrics=[Metric(name="sessions")],
            date_ranges=[dr],
        ))
        result = {}
        total  = 0
        for row in resp.rows:
            device = row.dimension_values[0].value.lower()
            sess   = int(row.metric_values[0].value)
            result[device] = sess
            total += sess
        result["_total"] = total
        return result

    def _ga4_geo(self, days: int = 7) -> list[dict]:
        """Top pays + villes par sessions."""
        from google.analytics.data_v1beta.types import RunReportRequest, Dimension, Metric, OrderBy
        dr, _ = self._date_range(days)
        resp  = self._ga4_client().run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name="country"), Dimension(name="city")],
            metrics=[Metric(name="sessions")],
            date_ranges=[dr],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            limit=8,
        ))
        return [
            {
                "country": row.dimension_values[0].value,
                "city":    row.dimension_values[1].value,
                "sessions": int(row.metric_values[0].value),
            }
            for row in resp.rows
        ]

    def _ga4_new_vs_returning(self, days: int = 7) -> dict:
        """Pourcentage nouveaux visiteurs vs retour."""
        from google.analytics.data_v1beta.types import RunReportRequest, Dimension, Metric
        dr, _ = self._date_range(days)
        resp  = self._ga4_client().run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name="newVsReturning")],
            metrics=[Metric(name="sessions"), Metric(name="activeUsers")],
            date_ranges=[dr],
        ))
        result = {}
        for row in resp.rows:
            label = row.dimension_values[0].value  # "new" or "returning"
            result[label] = {
                "sessions": int(row.metric_values[0].value),
                "users":    int(row.metric_values[1].value),
            }
        return result

    def _ga4_blog_performance(self, days: int = 7) -> list[dict]:
        """Performance des articles /journal/ — sessions totales et organiques."""
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric, OrderBy, FilterExpression, Filter
        )
        dr, _ = self._date_range(days)
        # Sessions totales par page /journal/
        resp_total = self._ga4_client().run_report(RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[Dimension(name="pagePath")],
            metrics=[Metric(name="sessions"), Metric(name="screenPageViews")],
            date_ranges=[dr],
            dimension_filter=FilterExpression(
                filter=Filter(
                    field_name="pagePath",
                    string_filter=Filter.StringFilter(
                        match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                        value="/journal/",
                    ),
                )
            ),
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
            limit=8,
        ))
        return [
            {
                "page":     row.dimension_values[0].value,
                "sessions": int(row.metric_values[0].value),
                "views":    int(row.metric_values[1].value),
            }
            for row in resp_total.rows
        ]

    def _ga4_losing_pages(self, days: int = 7) -> list[dict]:
        """Pages ayant perdu > 30% de trafic vs la période précédente."""
        from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric, OrderBy
        client = self._ga4_client()
        end    = datetime.today()
        start  = end - timedelta(days=days)
        prev_e = start - timedelta(days=1)
        prev_s = prev_e - timedelta(days=days)
        fmt    = lambda d: d.strftime("%Y-%m-%d")

        def _pages(s, e):
            resp = client.run_report(RunReportRequest(
                property=f"properties/{GA4_PROPERTY_ID}",
                dimensions=[Dimension(name="pagePath")],
                metrics=[Metric(name="sessions")],
                date_ranges=[DateRange(start_date=fmt(s), end_date=fmt(e))],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
                limit=30,
            ))
            return {row.dimension_values[0].value: int(row.metric_values[0].value) for row in resp.rows}

        curr = _pages(start, end)
        prev = _pages(prev_s, prev_e)

        losers = []
        for page, curr_sess in curr.items():
            prev_sess = prev.get(page, 0)
            if prev_sess >= 5 and curr_sess < prev_sess:
                delta = (curr_sess - prev_sess) / prev_sess * 100
                if delta <= -30:
                    losers.append({"page": page, "curr": curr_sess, "prev": prev_sess, "delta": delta})
        return sorted(losers, key=lambda x: x["delta"])[:5]

    # ── Helpers GSC ───────────────────────────────────────────────────────────

    def _gsc_keywords_full(self):
        """
        Retourne (keywords_current, kw_trend, opps).
        kw_trend = dict {query: {"curr_pos": float, "prev_pos": float, "delta": float, "ctr": float}}
        """
        from googleapiclient.discovery import build
        creds = get_service_account_creds(GSC_SCOPES)
        svc   = build("searchconsole", "v1", credentials=creds)

        end        = datetime.today()
        start      = end - timedelta(days=28)
        prev_end   = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=28)
        fmt        = lambda d: d.strftime("%Y-%m-%d")

        def _query(s, e, limit=200):
            return svc.searchanalytics().query(
                siteUrl=GSC_SITE_STUDIO,
                body={"startDate": fmt(s), "endDate": fmt(e), "dimensions": ["query"],
                      "rowLimit": limit},
            ).execute().get("rows", [])

        curr_rows = _query(start, end, limit=200)
        prev_rows = _query(prev_start, prev_end, limit=200)

        prev_map = {r["keys"][0]: r for r in prev_rows}

        # Tendance
        kw_trend = {}
        for r in curr_rows:
            kw          = r["keys"][0]
            curr_pos    = r["position"]
            prev_pos    = prev_map[kw]["position"] if kw in prev_map else None
            kw_trend[kw] = {
                "curr_pos": curr_pos,
                "prev_pos": prev_pos,
                "delta":    (prev_pos - curr_pos) if prev_pos else None,  # positif = amélioration
                "ctr":      r.get("ctr", 0),
                "clicks":   int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
            }

        # Top 10 par impressions pour affichage
        keywords = sorted(curr_rows, key=lambda x: x["impressions"], reverse=True)[:10]

        # Opportunités pos 4-20
        opps = sorted(
            [r for r in curr_rows if 4 <= r["position"] <= 20 and r["impressions"] >= 5],
            key=lambda x: x["impressions"], reverse=True
        )[:8]

        return keywords, kw_trend, opps

    def _gsc_archi(self) -> list:
        """Mots-clés du site Archi."""
        from googleapiclient.discovery import build
        if not GSC_SITE_ARCHI:
            return []
        creds = get_service_account_creds(GSC_SCOPES)
        svc   = build("searchconsole", "v1", credentials=creds)
        end   = datetime.today()
        start = end - timedelta(days=28)
        fmt   = lambda d: d.strftime("%Y-%m-%d")
        return svc.searchanalytics().query(
            siteUrl=GSC_SITE_ARCHI,
            body={"startDate": fmt(start), "endDate": fmt(end), "dimensions": ["query"],
                  "rowLimit": 8, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        ).execute().get("rows", [])

    # ── Analyse textuelle ─────────────────────────────────────────────────────

    def _analyse(self, totals, organic, direct, opps, events, device_split=None, new_ret=None) -> list[str]:
        sessions       = totals["sessions"]
        sessions_delta = totals["sessions_delta"]
        organic_pct    = organic / max(sessions, 1) * 100
        direct_pct     = direct  / max(sessions, 1) * 100
        bounce_pct     = totals.get("bounce_pct", totals["bounce"] * 100)
        duration_min   = totals["duration"] / 60

        lignes = []

        # Tendance générale
        if sessions_delta >= 10:
            lignes.append(f"📈 <strong>Bonne semaine</strong> — trafic en hausse de <strong>{sessions_delta:+.0f}%</strong> vs semaine précédente.")
        elif sessions_delta <= -10:
            lignes.append(f"📉 <strong>Attention</strong> — trafic en baisse de <strong>{sessions_delta:.0f}%</strong> vs semaine précédente.")
        else:
            lignes.append(f"→ Trafic stable (<strong>{sessions_delta:+.0f}%</strong> vs semaine précédente).")

        # Engagement
        if duration_min >= 3:
            lignes.append(f"✅ Visiteurs <strong>engagés</strong> — durée moy. <strong>{duration_min:.1f} min</strong>/session.")
        elif duration_min >= 1.5:
            lignes.append(f"→ Engagement modéré ({duration_min:.1f} min/session).")
        else:
            lignes.append(f"⚠️ Durée courte ({duration_min:.1f} min/session) — revoir l'accroche des pages.")

        # Rebond
        if bounce_pct < 35:
            lignes.append(f"✅ Taux de rebond excellent ({bounce_pct:.0f}%).")
        elif bounce_pct < 55:
            lignes.append(f"→ Taux de rebond acceptable ({bounce_pct:.0f}%).")
        else:
            lignes.append(f"⚠️ Taux de rebond élevé ({bounce_pct:.0f}%) — l'accroche ou la vitesse est problématique.")

        # SEO
        if organic_pct < 15:
            lignes.append(f"⚠️ Seulement <strong>{organic_pct:.0f}% de trafic organique</strong>. SEO = levier principal inexploité.")
        elif organic_pct < 35:
            lignes.append(f"→ Trafic organique à <strong>{organic_pct:.0f}%</strong> — en progression.")
        else:
            lignes.append(f"✅ Bon trafic organique ({organic_pct:.0f}%).")

        if direct_pct > 55:
            lignes.append(f"✅ Fort trafic direct ({direct_pct:.0f}%) — excellente notoriété de marque.")

        # Mobile
        if device_split:
            total_d  = max(device_split.get("_total", 1), 1)
            mob_pct  = device_split.get("mobile", 0) / total_d * 100
            desk_pct = device_split.get("desktop", 0) / total_d * 100
            if mob_pct > 60:
                lignes.append(f"📱 <strong>{mob_pct:.0f}% du trafic est mobile</strong> — la version mobile est ton produit principal.")
            elif mob_pct > 40:
                lignes.append(f"📱 Trafic mixte — {mob_pct:.0f}% mobile / {desk_pct:.0f}% desktop.")

        # Nouveaux vs retour
        if new_ret:
            new_sess = new_ret.get("new", {}).get("sessions", 0)
            ret_sess = new_ret.get("returning", {}).get("sessions", 0)
            total_nr = max(new_sess + ret_sess, 1)
            ret_pct  = ret_sess / total_nr * 100
            if ret_pct > 30:
                lignes.append(f"✅ <strong>{ret_pct:.0f}% de visiteurs fidèles</strong> — la rétention fonctionne.")
            elif ret_pct < 10:
                lignes.append(f"→ Peu de visiteurs de retour ({ret_pct:.0f}%) — travailler la fidélisation (newsletter, blog).")

        # Opportunités SEO
        if opps:
            lignes.append(f"🚀 <strong>{len(opps)} mots-clés en position 4-20</strong> pourraient passer en page 1.")
        else:
            lignes.append("→ Aucune opportunité SEO immédiate cette semaine.")

        # Conversions
        if events:
            ev = dict(events)
            form_start  = ev.get("form_start",  0)
            form_submit = ev.get("form_submit",  0)
            click_email = ev.get("click_email",  0)
            click_phone = ev.get("click_phone",  0)

            if form_submit > 0:
                lignes.append(
                    f"🎯 <strong>{form_submit} demande(s) de contact</strong> reçue(s) — "
                    f"<strong>ACTION REQUISE</strong> : répondre dans les 24h."
                )
            elif form_start > 0:
                lignes.append(
                    f"⚠️ <strong>{form_start} personne(s)</strong> ont commencé ton formulaire "
                    f"mais <strong>0 ne l'ont pas soumis</strong>. Simplifier = plus de leads."
                )
            else:
                lignes.append("→ Aucun événement de contact enregistré cette semaine.")

            if click_email or click_phone:
                lignes.append(f"📞 Contacts directs : {click_email} clic(s) email · {click_phone} clic(s) téléphone.")

        return lignes

    # ── Synthèse IA ───────────────────────────────────────────────────────────

    async def _synthesize_actions(self, totals, organic, direct, opps, events,
                                   device_split, geo, new_ret, blog_perf, losing_pages, kw_trend) -> list[str]:
        """Claude génère 3 actions prioritaires basées sur toutes les données."""
        try:
            import anthropic
            from config import ANTHROPIC_API_KEY

            # Construire un résumé compact pour Claude
            sessions = totals["sessions"]
            org_pct  = organic / max(sessions, 1) * 100
            dur_min  = totals["duration"] / 60
            bounce   = totals.get("bounce_pct", totals["bounce"] * 100)

            summary_lines = [
                f"Sessions: {sessions} ({totals['sessions_delta']:+.0f}% vs semaine précédente)",
                f"Trafic organique: {org_pct:.0f}%",
                f"Durée moyenne: {dur_min:.1f} min",
                f"Taux de rebond: {bounce:.0f}%",
            ]

            if device_split:
                total_d = max(device_split.get("_total", 1), 1)
                mob_pct = device_split.get("mobile", 0) / total_d * 100
                summary_lines.append(f"Trafic mobile: {mob_pct:.0f}%")

            if geo:
                top_geo = [f"{g['city']} ({g['country']}): {g['sessions']} sessions" for g in geo[:3]]
                summary_lines.append("Top villes: " + ", ".join(top_geo))

            if opps:
                top_opps = [f"{r['keys'][0]} (pos {r['position']:.0f}, {int(r['impressions'])} impr)" for r in opps[:3]]
                summary_lines.append("Opportunités SEO: " + ", ".join(top_opps))

            if losing_pages:
                summary_lines.append("Pages en baisse: " + ", ".join(f"{p['page']} ({p['delta']:+.0f}%)" for p in losing_pages[:3]))

            if events:
                ev = dict(events)
                summary_lines.append(f"Événements CTA: {ev}")

            if blog_perf:
                summary_lines.append("Blog top articles: " + ", ".join(f"{b['page']} ({b['sessions']} sess)" for b in blog_perf[:3]))

            # Mots-clés avec CTR faible malgré bonne position
            low_ctr = [
                f"{kw} (pos {v['curr_pos']:.0f}, CTR {v['ctr']*100:.1f}%)"
                for kw, v in list(kw_trend.items())[:20]
                if v["curr_pos"] <= 10 and v["ctr"] < 0.03 and v["impressions"] >= 5
            ][:3]
            if low_ctr:
                summary_lines.append("Mots-clés page 1 avec CTR < 3%: " + ", ".join(low_ctr))

            prompt = (
                "Tu es l'analyste SEO de Welldone Studio, une agence de design et photographie à Montréal. "
                "Basé sur ces données de la semaine, génère exactement 3 actions prioritaires concrètes et actionnables. "
                "Chaque action = 1 phrase courte, directe, avec un impact clair. "
                "Format: liste de 3 strings, sans numérotation ni bullet.\n\n"
                + "\n".join(summary_lines)
            )

            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp   = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            # Parser les lignes
            actions = [line.lstrip("•-123456789. ").strip() for line in raw.split("\n") if line.strip()][:3]
            return actions if actions else ["Analyser les opportunités SEO détectées cette semaine."]
        except Exception as e:
            log.warning(f"analytics._synthesize_actions error: {e}")
            return []

    # ── HTML Builder ──────────────────────────────────────────────────────────

    def _build_html(self, totals, sources_rows, pages_rows, keywords, kw_trend,
                    opps, archi_kw, analyse, actions,
                    start, end, days, events,
                    device_split, geo, new_ret, blog_perf, losing_pages) -> str:

        today = datetime.now().strftime("%d %b %Y")

        # Note CTR (pre-définie pour éviter backslash dans f-string)
        _ctr_note = (
            '<div style="background:#111;border:1px solid #222;border-radius:8px;padding:16px;margin:8px 0">'
            '<p style="margin:0 0 4px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:1px">Colonne CTR en rouge</p>'
            '<p style="margin:0;font-size:12px;color:#666">= tu es en page 1 mais moins de 1% des gens cliquent. '
            'Ton titre/description dans Google attire peu.</p>'
            '</div>'
        )

        # Styles
        ts  = "width:100%;border-collapse:collapse;margin:12px 0;font-size:13px"
        th  = "background:#1e1e1e;color:#aaa;padding:8px 10px;text-align:left;border-bottom:1px solid #333"
        td  = "padding:7px 10px;border-bottom:1px solid #1a1a1a;color:#ddd"
        tdr = td + ";text-align:right"
        tdc = td + ";text-align:center"

        def badge(val):
            if val > 5:  return f'<span style="color:#4ade80;font-size:11px"> ▲ {val:+.0f}%</span>'
            if val < -5: return f'<span style="color:#f87171;font-size:11px"> ▼ {val:.0f}%</span>'
            return f'<span style="color:#888;font-size:11px"> → {val:+.0f}%</span>'

        def h3(label):
            return f'<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:28px 0 8px">{label}</h3>'

        analyse_html = "".join(f"<p style='margin:8px 0;line-height:1.7'>{l}</p>" for l in analyse)

        # ── Sources
        src_rows = "".join(
            f'<tr><td style="{td}">{r.dimension_values[0].value}</td>'
            f'<td style="{tdr}">{int(r.metric_values[0].value):,}</td>'
            f'<td style="{tdr}">{int(r.metric_values[1].value):,}</td></tr>'
            for r in sources_rows
        )

        # ── Top pages
        pg_rows = "".join(
            f'<tr><td style="{td};font-family:monospace;font-size:12px">{r.dimension_values[0].value[:55]}</td>'
            f'<td style="{tdr}">{int(r.metric_values[0].value):,}</td>'
            f'<td style="{tdr}">{int(r.metric_values[1].value):,}</td></tr>'
            for r in pages_rows
        )

        # ── Mots-clés Studio avec CTR + tendance
        kw_rows = ""
        for r in keywords:
            kw       = r["keys"][0]
            impr     = int(r["impressions"])
            clics    = int(r["clicks"])
            pos      = r["position"]
            ctr      = r.get("ctr", 0) * 100
            trend    = kw_trend.get(kw, {})
            delta    = trend.get("delta")
            if delta is None:
                trend_html = '<span style="color:#555">—</span>'
            elif delta > 0.5:
                trend_html = f'<span style="color:#4ade80">▲ {delta:.1f}</span>'
            elif delta < -0.5:
                trend_html = f'<span style="color:#f87171">▼ {abs(delta):.1f}</span>'
            else:
                trend_html = f'<span style="color:#888">→</span>'

            ctr_color = "#f87171" if ctr < 1 and pos <= 10 else "#ddd"
            kw_rows += (
                f'<tr><td style="{td}">{kw[:45]}</td>'
                f'<td style="{tdr}">{impr:,}</td>'
                f'<td style="{tdr}">{clics}</td>'
                f'<td style="{tdr}">{pos:.0f}</td>'
                f'<td style="{tdr};color:{ctr_color}">{ctr:.1f}%</td>'
                f'<td style="{tdc}">{trend_html}</td></tr>'
            )

        # ── Opportunités SEO
        opp_rows = "".join(
            f'<tr><td style="{td}">{r["keys"][0][:45]}</td>'
            f'<td style="{tdr}">{int(r["impressions"]):,}</td>'
            f'<td style="{tdr}">{r["position"]:.0f}</td>'
            f'<td style="{tdr};color:#888">{r.get("ctr",0)*100:.1f}%</td></tr>'
            for r in opps
        )

        # ── GSC Archi
        archi_rows = ""
        if archi_kw:
            archi_rows = "".join(
                f'<tr><td style="{td}">{r["keys"][0][:45]}</td>'
                f'<td style="{tdr}">{int(r["impressions"]):,}</td>'
                f'<td style="{tdr}">{int(r["clicks"])}</td>'
                f'<td style="{tdr}">{r["position"]:.0f}</td>'
                f'<td style="{tdr};color:#888">{r.get("ctr",0)*100:.1f}%</td></tr>'
                for r in archi_kw
            )

        # ── Géographie
        geo_html = ""
        if geo:
            geo_rows = "".join(
                f'<tr><td style="{td}">{g["country"]}</td>'
                f'<td style="{td}">{g["city"]}</td>'
                f'<td style="{tdr}">{g["sessions"]:,}</td></tr>'
                for g in geo
            )
            geo_html = (
                h3("📍 Géographie — Top villes") +
                f'<table style="{ts}"><tr>'
                f'<th style="{th}">Pays</th><th style="{th}">Ville</th>'
                f'<th style="{th};text-align:right">Sessions</th></tr>'
                f'{geo_rows}</table>'
            )

        # ── Device split
        device_html = ""
        if device_split:
            total_d  = max(device_split.get("_total", 1), 1)
            mob_pct  = device_split.get("mobile", 0) / total_d * 100
            desk_pct = device_split.get("desktop", 0) / total_d * 100
            tab_pct  = device_split.get("tablet", 0) / total_d * 100
            device_html = (
                h3("📱 Mobile vs Desktop") +
                f'<div style="display:flex;gap:12px;margin-bottom:8px">'
                f'<div style="flex:1;background:#111;border:1px solid #222;border-radius:6px;padding:12px;text-align:center">'
                f'<div style="font-size:20px;font-weight:700;color:#fff">{desk_pct:.0f}%</div>'
                f'<div style="font-size:11px;color:#666;margin-top:2px">Desktop</div></div>'
                f'<div style="flex:1;background:#111;border:1px solid #222;border-radius:6px;padding:12px;text-align:center">'
                f'<div style="font-size:20px;font-weight:700;color:#fff">{mob_pct:.0f}%</div>'
                f'<div style="font-size:11px;color:#666;margin-top:2px">Mobile</div></div>'
                f'<div style="flex:1;background:#111;border:1px solid #222;border-radius:6px;padding:12px;text-align:center">'
                f'<div style="font-size:20px;font-weight:700;color:#fff">{tab_pct:.0f}%</div>'
                f'<div style="font-size:11px;color:#666;margin-top:2px">Tablette</div></div>'
                f'</div>'
            )

        # ── Nouveaux vs retour
        nvr_html = ""
        if new_ret:
            new_s = new_ret.get("new", {}).get("sessions", 0)
            ret_s = new_ret.get("returning", {}).get("sessions", 0)
            total = max(new_s + ret_s, 1)
            nvr_html = (
                h3("🔄 Nouveaux vs Visiteurs de retour") +
                f'<div style="display:flex;gap:12px;margin-bottom:8px">'
                f'<div style="flex:1;background:#111;border:1px solid #222;border-radius:6px;padding:12px;text-align:center">'
                f'<div style="font-size:20px;font-weight:700;color:#fff">{new_s/total*100:.0f}%</div>'
                f'<div style="font-size:11px;color:#666;margin-top:2px">Nouveaux ({new_s:,})</div></div>'
                f'<div style="flex:1;background:#111;border:1px solid #222;border-radius:6px;padding:12px;text-align:center">'
                f'<div style="font-size:20px;font-weight:700;color:#4ade80">{ret_s/total*100:.0f}%</div>'
                f'<div style="font-size:11px;color:#666;margin-top:2px">De retour ({ret_s:,})</div></div>'
                f'</div>'
            )

        # ── Blog performance
        blog_html = ""
        if blog_perf:
            blog_rows = "".join(
                f'<tr><td style="{td};font-family:monospace;font-size:12px">{b["page"][:55]}</td>'
                f'<td style="{tdr}">{b["sessions"]:,}</td>'
                f'<td style="{tdr}">{b["views"]:,}</td></tr>'
                for b in blog_perf
            )
            blog_html = (
                h3("✍️ Performance des articles de blog") +
                f'<table style="{ts}"><tr>'
                f'<th style="{th}">Article</th>'
                f'<th style="{th};text-align:right">Sessions</th>'
                f'<th style="{th};text-align:right">Pages vues</th></tr>'
                f'{blog_rows}</table>'
            )

        # ── Pages en baisse
        losing_html = ""
        if losing_pages:
            losing_rows = "".join(
                f'<tr><td style="{td};font-family:monospace;font-size:12px">{p["page"][:55]}</td>'
                f'<td style="{tdr}">{p["prev"]:,}</td>'
                f'<td style="{tdr}">{p["curr"]:,}</td>'
                f'<td style="{tdr};color:#f87171;font-weight:600">{p["delta"]:+.0f}%</td></tr>'
                for p in losing_pages
            )
            losing_html = (
                h3("📉 Pages qui ont perdu du trafic cette semaine") +
                f'<table style="{ts}"><tr>'
                f'<th style="{th}">Page</th>'
                f'<th style="{th};text-align:right">Sem. précédente</th>'
                f'<th style="{th};text-align:right">Cette semaine</th>'
                f'<th style="{th};text-align:right">Variation</th></tr>'
                f'{losing_rows}</table>'
            )

        # ── Actions prioritaires
        actions_html = ""
        if actions:
            items = "".join(
                f'<li style="margin:10px 0;padding:10px 12px;background:#1a1a1a;border-left:3px solid #f59e0b;'
                f'border-radius:4px;color:#e0e0e0;line-height:1.5">{a}</li>'
                for a in actions
            )
            actions_html = (
                h3("⚡ 3 Actions prioritaires cette semaine") +
                f'<ul style="list-style:none;padding:0;margin:0">{items}</ul>'
            )

        # ── Archi section
        archi_html = ""
        if archi_rows:
            archi_html = (
                h3("🏛️ Mots-clés Welldone Archi (28 jours)") +
                f'<table style="{ts}"><tr>'
                f'<th style="{th}">Mot-clé</th>'
                f'<th style="{th};text-align:right">Impr.</th>'
                f'<th style="{th};text-align:right">Clics</th>'
                f'<th style="{th};text-align:right">Pos.</th>'
                f'<th style="{th};text-align:right">CTR</th></tr>'
                f'{archi_rows}</table>'
            )

        return f"""<html><body style="margin:0;padding:0;background:#0d0d0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e0e0e0">
<div style="max-width:700px;margin:0 auto;padding:32px 24px">

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

  <div style="background:#111;border:1px solid #1e3a1e;border-left:3px solid #4ade80;border-radius:8px;padding:20px;margin-bottom:8px">
    <p style="margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#4ade80;font-weight:600">Analyse de la semaine</p>
    {analyse_html}
  </div>

  {actions_html}
  {device_html}
  {nvr_html}
  {geo_html}

  {h3("Sources de trafic")}
  <table style="{ts}"><tr><th style="{th}">Source</th><th style="{th};text-align:right">Sessions</th><th style="{th};text-align:right">Utilisateurs</th></tr>{src_rows}</table>

  {h3("Top pages")}
  <table style="{ts}"><tr><th style="{th}">Page</th><th style="{th};text-align:right">Sessions</th><th style="{th};text-align:right">Utilisateurs</th></tr>{pg_rows}</table>

  {blog_html}
  {losing_html}

  {h3("🔍 Mots-clés Google — Studio (28 jours)")}
  {'<table style="' + ts + '"><tr>'
    '<th style="' + th + '">Mot-clé</th>'
    '<th style="' + th + ';text-align:right">Impr.</th>'
    '<th style="' + th + ';text-align:right">Clics</th>'
    '<th style="' + th + ';text-align:right">Pos.</th>'
    '<th style="' + th + ';text-align:right">CTR</th>'
    '<th style="' + th + ';text-align:center">Tendance</th></tr>'
    + kw_rows + '</table>' if kw_rows else '<p style="color:#666;font-size:13px">Aucune donnée Search Console.</p>'}

  {_ctr_note if kw_rows else ""}

  {('<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:28px 0 8px">🚀 Opportunités SEO (pos. 4-20)</h3>'
    '<table style="' + ts + '"><tr>'
    '<th style="' + th + '">Mot-clé</th>'
    '<th style="' + th + ';text-align:right">Impressions</th>'
    '<th style="' + th + ';text-align:right">Position</th>'
    '<th style="' + th + ';text-align:right">CTR</th></tr>'
    + opp_rows + '</table>') if opp_rows else ''}

  {archi_html}
  {self._build_events_html(events or [], ts, th, td, tdr)}

  <div style="border-top:1px solid #1a1a1a;margin-top:40px;padding-top:16px;text-align:center">
    <p style="margin:0;color:#444;font-size:11px">Welldone Studio AI System · Rapport automatique chaque lundi 8h</p>
  </div>
</div></body></html>"""

    def _build_events_html(self, events, ts, th, td, tdr) -> str:
        if not events:
            return (
                '<p style="color:#555;font-size:13px;margin-top:24px">'
                'Aucun événement de conversion enregistré. Vérifie la config Google Tag.</p>'
            )
        rows = "".join(
            f'<tr><td style="{td};font-family:monospace;font-size:12px">{name}</td>'
            f'<td style="{tdr}">{count:,}</td></tr>'
            for name, count in events[:15]
        )
        return (
            '<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#888;margin:28px 0 8px">'
            '🎯 Événements & CTA (clics boutons, formulaires)</h3>'
            f'<table style="{ts}"><tr>'
            f'<th style="{th}">Événement</th>'
            f'<th style="{th};text-align:right">Déclenchements</th>'
            f'</tr>{rows}</table>'
        )

    # ── Envoi email ───────────────────────────────────────────────────────────

    def _send_email(self, html: str, start: str, end: str):
        import base64
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from googleapiclient.discovery import build
        from config import GMAIL_RECIPIENT
        from core.auth import get_oauth_creds

        today   = datetime.now().strftime("%d %b %Y")
        subject = f"📊 Welldone — Rapport semaine du {today}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_RECIPIENT
        msg["To"]      = GMAIL_RECIPIENT
        msg.attach(MIMEText(html, "html"))

        creds   = get_oauth_creds()
        service = build("gmail", "v1", credentials=creds)
        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


agent = AnalyticsAgent()
