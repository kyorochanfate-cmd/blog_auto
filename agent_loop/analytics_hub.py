"""A: GA4 → Gemini 整形 → スプレッドシート「①現状データ」追記。

環境変数:
  GA4_PROPERTY_ID      : GA4 のプロパティID (数値)
  GA4_LOOKBACK_DAYS    : 取得日数 (デフォルト 7)
  SPREADSHEET_ID       : 追記先スプレッドシート
  GEMINI_API_KEY       : Gemini API キー
  GEMINI_MODEL         : 既定 'gemini-3.5-flash-lite'
  GOOGLE_APPLICATION_CREDENTIALS : サービスアカウントJSON (GA4 / Sheets 両方の権限)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest,
)
from google import genai

from . import sheets

log = logging.getLogger(__name__)


def _fetch_ga4_rows() -> list[dict]:
    property_id = os.environ['GA4_PROPERTY_ID']
    lookback = int(os.environ.get('GA4_LOOKBACK_DAYS', '7'))

    client = BetaAnalyticsDataClient()
    req = RunReportRequest(
        property=f'properties/{property_id}',
        date_ranges=[DateRange(start_date=f'{lookback}daysAgo', end_date='today')],
        dimensions=[
            Dimension(name='date'),
            Dimension(name='pagePath'),
            Dimension(name='pageTitle'),
        ],
        metrics=[
            Metric(name='screenPageViews'),
            Metric(name='userEngagementDuration'),
            Metric(name='activeUsers'),
        ],
        limit=10000,
    )
    resp = client.run_report(req)

    rows: list[dict] = []
    for r in resp.rows:
        date_s, path, title = (d.value for d in r.dimension_values)
        views_s, eng_s, users_s = (m.value for m in r.metric_values)
        views = int(views_s or 0)
        engagement = float(eng_s or 0)
        users = int(users_s or 0)
        avg_eng = engagement / users if users else 0.0
        rows.append({
            'date': date_s,
            'page_path': path,
            'page_title': title,
            'views': views,
            'avg_engagement_sec': round(avg_eng, 1),
        })
    rows.sort(key=lambda x: (x['date'], -x['views']))
    return rows


_SUMMARY_PROMPT = """以下は当ブログのGA4アクセスデータ（直近）です。
読者傾向を1行のCSVセル向け日本語サマリ（80文字以内・改行/カンマ禁止）で出力してください。
出力はサマリ本文のみ。前置きや引用符は不要。

データ:
{rows_json}
"""


def _gemini_summary(rows: list[dict]) -> str:
    if not rows:
        return ''
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    top = rows[:30]
    prompt = _SUMMARY_PROMPT.format(rows_json=json.dumps(top, ensure_ascii=False))
    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or '').strip().replace('\n', ' ').replace(',', '、')
        return text[:120]
    except Exception as e:
        log.warning('Gemini summary failed: %s', e)
        return ''


def run() -> int:
    """GA4 を取得してシートに追記。追記した行数を返す。"""
    sheets.ensure_headers()
    rows = _fetch_ga4_rows()
    if not rows:
        log.info('GA4 returned 0 rows')
        return 0

    summary = _gemini_summary(rows)
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    sheet_rows = [
        [
            ts, r['date'], r['page_path'], r['page_title'],
            r['views'], r['avg_engagement_sec'],
            summary if i == 0 else '',
        ]
        for i, r in enumerate(rows)
    ]
    sheets.append_current(sheet_rows)
    log.info('Appended %d rows to current-data sheet', len(sheet_rows))
    return len(sheet_rows)
