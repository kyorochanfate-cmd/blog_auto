"""ブログの低価値記事を判定して削除する。

ルール:
- 投稿から **3日以上経過** している
- かつ次のいずれか:
  - GSC クリック数 ≤ 1 (=検索流入ほぼゼロ)
  - 本文に画像が0枚

両条件を満たす記事のみ削除対象。新規記事(3日以内)は対象外。

ハブ記事は削除しないようタイトルに「ガイド」「2026年版」を含むかで除外可能 (オプション)。
"""
import re
import requests
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

from src import affiliate_upgrade
from src import search_console


_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom',
            'app': 'http://www.w3.org/2007/app'}


def _list_entries_with_date(blog, page_url=None):
    """エントリ一覧 (公開日付付き)。"""
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']
    domain = blog['hatena_blog_domain']
    url = page_url or f'https://blog.hatena.ne.jp/{hatena_id}/{domain}/atom/entry'
    r = requests.get(url, auth=(hatena_id, api_key), timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    entries = []
    for entry_el in root.findall('atom:entry', _ATOM_NS):
        title_el = entry_el.find('atom:title', _ATOM_NS)
        title = (title_el.text or '').strip() if title_el is not None else ''
        edit_link = ''
        alt_link = ''
        for link in entry_el.findall('atom:link', _ATOM_NS):
            rel = link.get('rel', '')
            href = link.get('href', '')
            if rel == 'edit':
                edit_link = href
            elif rel == 'alternate':
                alt_link = href
        # 公開日付 (published)
        pub_el = entry_el.find('atom:published', _ATOM_NS)
        pub_str = (pub_el.text or '') if pub_el is not None else ''
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
        except Exception:
            pub_dt = None
        # draft
        draft = 'no'
        ctrl = entry_el.find('app:control', _ATOM_NS)
        if ctrl is not None:
            d = ctrl.find('app:draft', _ATOM_NS)
            if d is not None and d.text:
                draft = d.text.strip()
        entries.append({
            'title': title, 'edit_url': edit_link,
            'alternate_url': alt_link,
            'published': pub_dt,
            'draft': draft,
        })
    next_link = ''
    for link in root.findall('atom:link', _ATOM_NS):
        if link.get('rel') == 'next':
            next_link = link.get('href', '')
            break
    return entries, next_link


def _list_all_entries(blog, max_pages=50):
    """ブログの全公開エントリを取得 (最大 max_pages = ~350記事まで)。"""
    all_entries = []
    page_url = None
    for _ in range(max_pages):
        entries, next_link = _list_entries_with_date(blog, page_url)
        all_entries.extend(entries)
        if not next_link:
            break
        page_url = next_link
    return [e for e in all_entries if e['draft'] != 'yes']


def _has_image(body):
    """本文に画像 ![](url) が含まれるか。"""
    if not body:
        return False
    # Markdown 画像 OR HTML img タグ
    if re.search(r'!\[[^\]]*\]\([^)\s]+', body):
        return True
    if re.search(r'<img[^>]+src="[^"]+"', body):
        return True
    return False


def find_low_value_articles(blog, days_age=3, days_gsc=28, hub_keywords=None):
    """削除候補リストを返す。

    Args:
        blog: ブログ設定
        days_age: 投稿からこの日数以上経過した記事のみ判定対象 (デフォルト3日)
        days_gsc: GSC を何日分集計するか (デフォルト28日)
        hub_keywords: タイトルにこのキーワードを含む記事は保護 (ハブ記事の自動除外用)

    Returns:
        {
            'candidates': [{
                'title','url','edit_url','published','clicks','impressions',
                'has_image','reasons':['low_clicks','no_image']
            }, ...],
            'total_entries': N,
            'date_range': '...',
        }
    """
    if hub_keywords is None:
        hub_keywords = ['完全ガイド', '2026年版', '徹底解説']

    domain = blog.get('hatena_blog_domain', '')
    site_url = f'https://{domain}/'

    # 1. 全エントリ取得 (Hatena AtomPub)
    entries = _list_all_entries(blog)

    # 2. GSC データ取得 (page別 clicks)
    sc_data = search_console.query_pages(site_url, days=days_gsc, row_limit=2000)
    clicks_by_url = {}
    impressions_by_url = {}
    if sc_data.get('ok'):
        for row in sc_data['rows']:
            page = (row.get('page') or '').rstrip('/')
            clicks_by_url[page] = clicks_by_url.get(page, 0) + row.get('clicks', 0)
            impressions_by_url[page] = impressions_by_url.get(page, 0) + row.get('impressions', 0)

    # 3. 各記事を判定
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_age)
    candidates = []
    for e in entries:
        pub = e.get('published')
        if not pub or pub > cutoff:
            continue  # 3日以内は対象外

        # ハブ保護
        title = e.get('title', '')
        if any(k in title for k in hub_keywords):
            continue

        # 本文取得 (画像チェック用)
        try:
            entry_full = affiliate_upgrade.fetch_entry(blog, e['edit_url'])
            body = entry_full.get('content') or ''
        except Exception as ex:
            print(f'[pruner] fetch failed for {e["alternate_url"]}: {ex}', flush=True)
            continue

        has_img = _has_image(body)
        url_key = (e.get('alternate_url') or '').rstrip('/')
        clicks = clicks_by_url.get(url_key, 0)
        impressions = impressions_by_url.get(url_key, 0)

        reasons = []
        if clicks <= 1:
            reasons.append('low_clicks')
        if not has_img:
            reasons.append('no_image')

        if not reasons:
            continue  # どちらも該当しないので保護

        candidates.append({
            'title': title,
            'url': e['alternate_url'],
            'edit_url': e['edit_url'],
            'published': pub.isoformat() if pub else '',
            'clicks': clicks,
            'impressions': impressions,
            'has_image': has_img,
            'body_length': len(body),
            'reasons': reasons,
        })

    return {
        'total_entries': len(entries),
        'candidates_count': len(candidates),
        'candidates': candidates,
        'date_range': f'最終 {days_gsc} 日間のGSCデータ参照',
    }


def delete_article(blog, edit_url):
    """Hatena AtomPub DELETE で記事削除。"""
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']
    r = requests.delete(edit_url, auth=(hatena_id, api_key), timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f'delete failed HTTP {r.status_code}: {r.text[:300]}')
    return True


def prune_articles(blog, days_age=3, days_gsc=28, dry_run=True, limit=None,
                    hub_keywords=None):
    """低価値記事を検出して(必要なら)削除。

    Returns:
        {
            'dry_run': bool,
            'total_entries': N, 'candidates_count': N,
            'deleted': N, 'failed': N,
            'candidates': [...],
            'errors': [...]
        }
    """
    found = find_low_value_articles(
        blog, days_age=days_age, days_gsc=days_gsc, hub_keywords=hub_keywords,
    )
    candidates = found['candidates']
    if limit is not None:
        candidates = candidates[:limit]

    result = {
        'dry_run': dry_run,
        'total_entries': found['total_entries'],
        'candidates_count': found['candidates_count'],
        'candidates': candidates,
        'deleted': 0, 'failed': 0,
        'errors': [],
    }

    if dry_run:
        return result

    # 本番削除 (Hatena 削除 + Firestore record 削除を同時に)
    from webapp import blogs as _blogs
    for c in candidates:
        try:
            delete_article(blog, c['edit_url'])
            # Firestore の published_articles record も削除 (関連記事リンクの死リンク化防止)
            try:
                _blogs.delete_published_article_record(c['url'])
            except Exception as e:
                print(f'[pruner] firestore record cleanup failed for {c["url"]}: {e}', flush=True)
            result['deleted'] += 1
            print(f'[pruner] deleted: {c["url"]}', flush=True)
        except Exception as e:
            result['failed'] += 1
            result['errors'].append({'url': c['url'], 'error': str(e)})
            print(f'[pruner] FAIL: {c["url"]} {e}', flush=True)

    return result
