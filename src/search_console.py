"""Google Search Console (Search Analytics) クライアント。

記事ごと・クエリごとの「インプレッション・クリック・CTR・順位」を取得して
リライト候補を絞り込むのに使う。

認証は google_indexing.py と同じ Service Account JSON を流用 (GOOGLE_INDEXING_SA_JSON)。
Search Console プロパティに SA メールアドレスを「オーナー」追加してあれば、
Indexing API と Search Analytics の両方が同じ SA で読める。
"""
import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
import google.auth.transport.requests


_SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']
_BASE = 'https://www.googleapis.com/webmasters/v3/sites'

_session_cache = {}


def is_configured():
    return bool(os.environ.get('GOOGLE_INDEXING_SA_JSON'))


def _session():
    if 'sc' in _session_cache:
        return _session_cache['sc']
    sa_json = os.environ.get('GOOGLE_INDEXING_SA_JSON', '').strip()
    if not sa_json:
        return None
    try:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    except Exception as e:
        print(f'[sc] credentials init failed: {e}', flush=True)
        return None
    sess = google.auth.transport.requests.AuthorizedSession(creds)
    _session_cache['sc'] = sess
    return sess


def list_sites():
    """SA から見えるサイト一覧 (デバッグ・確認用)。"""
    sess = _session()
    if sess is None:
        return {'ok': False, 'error': 'no_credentials'}
    r = sess.get('https://www.googleapis.com/webmasters/v3/sites', timeout=20)
    if r.status_code >= 400:
        return {'ok': False, 'error': f'HTTP {r.status_code}', 'detail': r.text[:300]}
    return {'ok': True, 'sites': r.json().get('siteEntry', [])}


def query_pages(site_url, days=28, row_limit=500):
    """ページ別 + クエリ別の検索パフォーマンスを取得。

    Args:
        site_url: GSC 登録のサイト URL (例: 'https://tako-karamaru.hatenablog.com/')
        days: 何日前まで遡るか (1〜90 程度)
        row_limit: 取得行数 (上限 25000)

    Returns:
        {'ok': True, 'rows': [{'page','query','clicks','impressions','ctr','position'}, ...]}
        または {'ok': False, 'error': ...}
    """
    sess = _session()
    if sess is None:
        return {'ok': False, 'error': 'no_credentials'}

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    body = {
        'startDate': start_date.isoformat(),
        'endDate': end_date.isoformat(),
        'dimensions': ['page', 'query'],
        'rowLimit': row_limit,
        'startRow': 0,
    }
    from urllib.parse import quote
    url = f'{_BASE}/{quote(site_url, safe="")}/searchAnalytics/query'
    try:
        r = sess.post(url, json=body, timeout=30)
    except Exception as e:
        return {'ok': False, 'error': f'request_exception: {e}'}
    if r.status_code >= 400:
        return {'ok': False, 'error': f'HTTP {r.status_code}', 'detail': r.text[:500]}

    data = r.json() or {}
    rows = []
    for row in data.get('rows', []):
        keys = row.get('keys') or []
        if len(keys) < 2:
            continue
        rows.append({
            'page': keys[0],
            'query': keys[1],
            'clicks': row.get('clicks', 0),
            'impressions': row.get('impressions', 0),
            'ctr': row.get('ctr', 0.0),
            'position': row.get('position', 0.0),
        })
    return {'ok': True, 'rows': rows, 'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()}


def find_rewrite_candidates(rows, min_impressions=30, ctr_threshold=0.02, position_max=20):
    """リライト価値の高い (page, query) ペアを抽出する。

    狙い: インプレッションそこそこ多い × CTR 低い × 順位そこそこ良い → タイトル変えれば CTR ブレイクする可能性大

    Args:
        rows: query_pages() の返り値の rows
        min_impressions: 最低インプレッション数
        ctr_threshold: これより CTR が低いものを対象 (デフォルト 2%)
        position_max: これより順位が悪いものは除外 (上位だけ対象)

    Returns:
        [{'page','query','clicks','impressions','ctr','position','score'}, ...]
        score 降順。各ページごとに最高スコアのクエリ1件のみ。
    """
    by_page = {}
    for row in rows:
        if row['impressions'] < min_impressions:
            continue
        if row['ctr'] > ctr_threshold:
            continue
        if row['position'] > position_max:
            continue
        # スコア: インプ × (理想CTR - 実CTR) × 順位ボーナス
        # 順位が良いほどタイトルだけで上がる余地が大きい
        ideal_ctr = max(0.05, _expected_ctr_by_position(row['position']))
        gain = max(0, ideal_ctr - row['ctr'])
        position_bonus = max(0.3, 1 - row['position'] / 25)
        score = row['impressions'] * gain * position_bonus
        if score <= 0:
            continue
        key = row['page']
        if key not in by_page or by_page[key]['score'] < score:
            by_page[key] = {**row, 'score': score}

    cands = sorted(by_page.values(), key=lambda x: -x['score'])
    return cands


# 順位別の期待CTR (業界一般値、AWR/SISTRIX 統計を簡略化)
_EXPECTED_CTR = [
    (1, 0.28), (2, 0.15), (3, 0.10), (4, 0.07), (5, 0.06),
    (6, 0.045), (7, 0.035), (8, 0.028), (9, 0.022), (10, 0.018),
    (15, 0.012), (20, 0.008),
]


def _expected_ctr_by_position(position):
    if position <= 1:
        return _EXPECTED_CTR[0][1]
    for (pos, ctr) in _EXPECTED_CTR:
        if position <= pos:
            return ctr
    return 0.005
