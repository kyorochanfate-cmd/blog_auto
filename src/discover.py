"""はてなブックマーク IT/テクノロジーから Hatena Blog 内の記事を抽出。

スター・購読を手動で行うための候補を提示する。
BOTではなく「人間が読んで価値があれば手動でスター」を支援するUI用のデータ供給。
"""
import re
import sys
from urllib.parse import urlparse
import feedparser
import requests


_FEEDS = [
    ('はてブIT 人気', 'https://b.hatena.ne.jp/hotentry/it.rss'),
    ('はてブIT 新着', 'https://b.hatena.ne.jp/entrylist/it.rss'),
]

_HATENA_BLOG_DOMAINS = (
    'hatenablog.com', 'hatenablog.jp', 'hateblo.jp',
    'hatenadiary.com', 'hatenadiary.jp', 'hatenablog.org',
)


def _is_hatena_blog(url):
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(d in host for d in _HATENA_BLOG_DOMAINS)


def _extract_author_id(url):
    """https://tako.hatenablog.com/entry/... → 'tako' (はてなID)"""
    try:
        host = urlparse(url).netloc.lower()
        return host.split('.')[0]
    except Exception:
        return ''


def _extract_blog_home(url):
    """https://tako.hatenablog.com/entry/2026/.../foo → https://tako.hatenablog.com/"""
    try:
        p = urlparse(url)
        return f'{p.scheme}://{p.netloc}/'
    except Exception:
        return ''


def _strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()


def fetch_engagement_targets(per_feed=30, my_blog_keywords=None):
    """はてブから Hatena Blog 投稿のみフィルタして返す。

    my_blog_keywords が渡されたらキーワード一致度で score を付ける。
    戻り値: [{'title','url','summary','author_id','source','score'}, ...]
    """
    items = []
    seen = set()
    for source, url in _FEEDS:
        try:
            r = requests.get(url, timeout=12, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)',
                'Accept': 'application/rss+xml,application/xml,*/*',
            })
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for entry in feed.entries[:per_feed]:
                u = entry.get('link', '')
                if not _is_hatena_blog(u) or u in seen:
                    continue
                seen.add(u)
                title = entry.get('title', '') or ''
                summary = _strip_html(entry.get('summary', '') or '')[:200]
                author = _extract_author_id(u)

                # キーワード一致度スコア
                score = 0
                if my_blog_keywords:
                    text = (title + ' ' + summary).lower()
                    score = sum(1 for kw in my_blog_keywords if kw and kw.lower() in text)

                items.append({
                    'title': title,
                    'url': u,
                    'summary': summary,
                    'author_id': author,
                    'blog_home': _extract_blog_home(u),
                    'source': source,
                    'score': score,
                })
            print(f'[discover] {source}: {len([i for i in items if i["source"]==source])} hatena items', flush=True)
        except Exception as e:
            print(f'[discover] ⚠ {source} failed: {e}', file=sys.stderr, flush=True)

    # スコア → 元の順序の順でソート
    items.sort(key=lambda x: -x['score'])
    return items


def get_user_keywords_from_blog(blog_id, limit_articles=30):
    """投稿済み記事のキーワードを集約して、ユーザーの興味キーワードを返す。"""
    from webapp import blogs as blog_store
    articles = blog_store.list_published_articles(blog_id, limit=limit_articles)
    keyword_freq = {}
    for a in articles:
        for kw in a.get('keywords', []):
            keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
    # 頻出 TOP20
    sorted_kws = sorted(keyword_freq.items(), key=lambda x: -x[1])[:20]
    return [k for k, _ in sorted_kws]
