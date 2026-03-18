#!/usr/bin/env python3
"""
Google Analytics 4 — Welldone Studio
Property ID: 522467276
"""

import json
import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Dimension, Metric, OrderBy
)

PROPERTY_ID = "522467276"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
CREDENTIALS_FILE = os.path.expanduser("~/.config/gws/client_secret.json")
TOKEN_FILE = os.path.expanduser("~/.config/gws/analytics_token.json")


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


def run_report(start_date="30daysAgo", end_date="today"):
    creds = get_credentials()
    client = BetaAnalyticsDataClient(credentials=creds)

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
            Metric(name="bounceRate"),
            Metric(name="averageSessionDuration"),
        ],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=20,
    )

    response = client.run_report(request)

    print(f"\n📊 Rapport GA4 — Welldone Studio ({start_date} → {end_date})\n")
    print(f"{'Page':<50} {'Sessions':>10} {'Users':>8} {'Vues':>8} {'Bounce':>8} {'Durée':>8}")
    print("-" * 100)

    for row in response.rows:
        path = row.dimension_values[0].value[:48]
        sessions = row.metric_values[0].value
        users = row.metric_values[1].value
        views = row.metric_values[2].value
        bounce = f"{float(row.metric_values[3].value)*100:.1f}%"
        duration = f"{float(row.metric_values[4].value):.0f}s"
        print(f"{path:<50} {sessions:>10} {users:>8} {views:>8} {bounce:>8} {duration:>8}")

    # Totaux
    print("-" * 100)
    total_sessions = sum(int(r.metric_values[0].value) for r in response.rows)
    total_users = sum(int(r.metric_values[1].value) for r in response.rows)
    total_views = sum(int(r.metric_values[2].value) for r in response.rows)
    print(f"{'TOTAL (top 20)':<50} {total_sessions:>10} {total_users:>8} {total_views:>8}")


def traffic_by_source(start_date="30daysAgo", end_date="today"):
    creds = get_credentials()
    client = BetaAnalyticsDataClient(credentials=creds)

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers"), Metric(name="conversions")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
    )

    response = client.run_report(request)

    print(f"\n🔍 Trafic par source ({start_date} → {end_date})\n")
    print(f"{'Source':<30} {'Sessions':>10} {'Users':>8} {'Conv.':>8}")
    print("-" * 60)
    for row in response.rows:
        source = row.dimension_values[0].value
        sessions = row.metric_values[0].value
        users = row.metric_values[1].value
        conv = row.metric_values[2].value
        print(f"{source:<30} {sessions:>10} {users:>8} {conv:>8}")


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "pages"
    start = sys.argv[2] if len(sys.argv) > 2 else "30daysAgo"
    end = sys.argv[3] if len(sys.argv) > 3 else "today"

    if mode == "sources":
        traffic_by_source(start, end)
    else:
        run_report(start, end)
