"""Google Indexing API クライアント。

記事公開直後に Google へ「このURLをインデックスして」と通知する。

公式は Job Posting / Live Stream 専用とされているが、実際は一般記事URLも
受理され、検索インデックスが大幅に速くなる(2-24時間でインデックス入り)。
規約的にはグレーだがペナルティ事例は無く、SEO業界で広く使われている。

環境変数:
  GOOGLE_INDEXING_SA_JSON  Service Account JSON (1行のcompact形式)
未設定の場合は何もせず黙ってスキップ。

Search Console 側で、この Service Account のメールアドレスを
「オーナー」として追加しておく必要がある (1回だけの手動作業)。
"""
import json
import os
from google.oauth2 import service_account
import google.auth.transport.requests


_SCOPES = ['https://www.googleapis.com/auth/indexing']
_ENDPOINT = 'https://indexing.googleapis.com/v3/urlNotifications:publish'

# 認証セッションを使い回す (起動コストが高いので)
_cached_session = None


def is_configured():
    return bool(os.environ.get('GOOGLE_INDEXING_SA_JSON'))


def _get_session():
    global _cached_session
    if _cached_session is not None:
        return _cached_session

    sa_json = os.environ.get('GOOGLE_INDEXING_SA_JSON', '').strip()
    if not sa_json:
        return None

    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as e:
        print(f'[indexing] SA JSON parse error: {e}', flush=True)
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    except Exception as e:
        print(f'[indexing] credentials init failed: {e}', flush=True)
        return None

    _cached_session = google.auth.transport.requests.AuthorizedSession(creds)
    return _cached_session


def notify_url(url, action='URL_UPDATED'):
    """URLを Google Indexing API に通知する。

    Args:
        url: 通知する記事URL
        action: 'URL_UPDATED' (新規/更新) or 'URL_DELETED'
    Returns:
        {'ok': True, 'response': ...} or {'ok': False, 'error': ...}
    例外を投げない (publish フローをブロックしないため)。
    """
    if not url:
        return {'ok': False, 'error': 'no_url'}
    if not is_configured():
        return {'ok': False, 'error': 'not_configured'}

    session = _get_session()
    if session is None:
        return {'ok': False, 'error': 'credentials_unavailable'}

    try:
        r = session.post(_ENDPOINT, json={'url': url, 'type': action}, timeout=15)
    except Exception as e:
        print(f'[indexing] request error: {e}', flush=True)
        return {'ok': False, 'error': f'request_exception: {e}'}

    if r.status_code < 400:
        try:
            return {'ok': True, 'response': r.json()}
        except Exception:
            return {'ok': True, 'response': r.text[:200]}

    body = r.text[:300]
    print(f'[indexing] HTTP {r.status_code}: {body}', flush=True)
    return {'ok': False, 'error': f'HTTP {r.status_code}', 'detail': body}
