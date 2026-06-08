"""D: スプレッドシート「③投稿待ち記事」→ Hatena AtomPub 自動投稿。

環境変数:
  HATENA_USER_ID, HATENA_BLOG_ID, HATENA_API_KEY
  HATENA_DRAFT  : '1' なら下書き保存 (既定: '0' = 公開)
  SPREADSHEET_ID, GOOGLE_APPLICATION_CREDENTIALS
  RAKUTEN_APP_ID, RAKUTEN_AFFILIATE_ID, AMAZON_AFFILIATE_TAG
    : [PRODUCT_CARD: 商品名] を実商品カードに変換するため。未設定ならカード変換スキップ
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import markdown as md_lib
import requests
from requests.auth import HTTPBasicAuth

from . import sheets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from src import product_search  # type: ignore
except Exception as _e:  # pragma: no cover
    product_search = None
    logging.getLogger(__name__).warning('product_search unavailable: %s', _e)

log = logging.getLogger(__name__)

ATOM_ENDPOINT = 'https://blog.hatena.ne.jp/{user}/{blog}/atom/entry'


def _convert_product_cards(body_md: str) -> str:
    """[PRODUCT_CARD: 商品名] を実商品カードHTMLに置換。

    既存の src/product_search.replace_placeholders() を再利用。
    必要な認証情報は環境変数から組み立てた blog dict として渡す。
    """
    if product_search is None or '[PRODUCT_CARD:' not in body_md:
        return body_md
    blog = {
        'rakuten_app_id': os.environ.get('RAKUTEN_APP_ID', ''),
        'rakuten_affiliate_id': os.environ.get('RAKUTEN_AFFILIATE_ID', ''),
        'rakuten_access_key': os.environ.get('RAKUTEN_ACCESS_KEY', ''),
        'amazon_affiliate_tag': os.environ.get('AMAZON_AFFILIATE_TAG', ''),
    }
    if not blog['rakuten_app_id']:
        log.warning('RAKUTEN_APP_ID 未設定。商品カード変換をスキップ。')
        return body_md
    try:
        return product_search.replace_placeholders(body_md, blog)
    except Exception as e:
        log.exception('product_card conversion failed: %s', e)
        return body_md


def _build_entry_xml(title: str, body_md: str, draft: bool) -> bytes:
    body_md = _convert_product_cards(body_md)
    html = md_lib.markdown(body_md, extensions=['fenced_code', 'tables'])
    draft_flag = 'yes' if draft else 'no'
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://www.w3.org/2005/Atom"
       xmlns:app="http://www.w3.org/2007/app">
  <title>{escape(title)}</title>
  <content type="text/html">{escape(html)}</content>
  <app:control>
    <app:draft>{draft_flag}</app:draft>
  </app:control>
</entry>
"""
    return xml.encode('utf-8')


_EDIT_LINK_RE = re.compile(
    r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', re.IGNORECASE
)


def _post_entry(title: str, body_md: str) -> str:
    user = os.environ['HATENA_USER_ID']
    blog = os.environ['HATENA_BLOG_ID']
    api_key = os.environ['HATENA_API_KEY']
    draft = os.environ.get('HATENA_DRAFT', '0') == '1'

    url = ATOM_ENDPOINT.format(user=user, blog=blog)
    body = _build_entry_xml(title, body_md, draft)
    resp = requests.post(
        url,
        data=body,
        auth=HTTPBasicAuth(user, api_key),
        headers={'Content-Type': 'application/atom+xml; charset=utf-8'},
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'Hatena AtomPub {resp.status_code}: {resp.text[:300]}')

    m = _EDIT_LINK_RE.search(resp.text)
    return m.group(1) if m else ''


def run() -> tuple[int, int]:
    """投稿待ち全件を順次投稿。 (success, failed) を返す。"""
    sheets.ensure_headers()
    pending = sheets.fetch_pending_articles()
    if not pending:
        log.info('No pending articles')
        return (0, 0)

    ok = ng = 0
    for art in pending:
        try:
            hatena_url = _post_entry(art.title, art.body_md)
            ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
            sheets.mark_posted(art.row, hatena_url, ts)
            if hatena_url:
                try:
                    sheets.record_published(art.title, hatena_url, ts)
                except Exception as e:
                    log.warning('record_published failed: %s', e)
            log.info('Posted row=%d title=%s url=%s', art.row, art.title, hatena_url)
            ok += 1
        except Exception as e:
            log.exception('Publish failed row=%d', art.row)
            sheets.mark_failed(art.row, str(e))
            ng += 1
    return (ok, ng)
