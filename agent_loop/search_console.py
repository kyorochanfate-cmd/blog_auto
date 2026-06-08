"""Google Search Console から検索クエリデータを取得する。

環境変数:
  GSC_SITE_URL : Search Console に登録したサイトURL
                 例: 'https://tako-karamaru.hatenablog.com/'
                 ドメインプロパティ形式なら 'sc-domain:tako-karamaru.hatenablog.com'
  GSC_LOOKBACK_DAYS : 何日前までを集計対象にするか (既定 28)
  GOOGLE_APPLICATION_CREDENTIALS : サービスアカウントJSON
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']


def _service():
    creds_path = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build('searchconsole', 'v1', credentials=creds, cache_discovery=False)


def fetch_top_queries(limit: int = 50) -> list[dict[str, Any]]:
    """検索クエリを clicks 降順で取得。

    Returns:
      [{query, clicks, impressions, ctr, position, page (代表)}, ...]
      未設定/失敗時は []
    """
    site_url = os.environ.get('GSC_SITE_URL', '').strip()
    if not site_url:
        log.info('GSC_SITE_URL 未設定。Search Console データ取得をスキップ。')
        return []

    lookback = int(os.environ.get('GSC_LOOKBACK_DAYS', '28'))
    # Search Console は当日と前日のデータが遅延するので2日前まで
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=lookback)

    body = {
        'startDate': start.isoformat(),
        'endDate': end.isoformat(),
        'dimensions': ['query'],
        'rowLimit': max(1, min(limit, 100)),
        'orderBy': [{'fieldName': 'clicks', 'sortOrder': 'DESCENDING'}],
    }

    svc = _service()
    try:
        resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except Exception as e:
        log.exception('GSC query failed: %s', e)
        return []

    rows = resp.get('rows') or []
    out = []
    for r in rows:
        keys = r.get('keys') or []
        if not keys:
            continue
        out.append({
            'query': keys[0],
            'clicks': int(r.get('clicks', 0)),
            'impressions': int(r.get('impressions', 0)),
            'ctr': round(float(r.get('ctr', 0)) * 100, 2),
            'position': round(float(r.get('position', 0)), 1),
        })
    log.info('GSC fetched %d queries (window=%d days)', len(out), lookback)
    return out


def format_for_prompt(queries: list[dict[str, Any]], top: int = 30) -> str:
    """Gemini プロンプト用のCSV風1行に整形。"""
    if not queries:
        return '(GSCデータなし)'
    lines = ['query, clicks, impressions, ctr%, position']
    for q in queries[:top]:
        lines.append(
            f"{q['query']}, {q['clicks']}, {q['impressions']}, {q['ctr']}, {q['position']}"
        )
    return '\n'.join(lines)
