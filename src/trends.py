"""今ネットで話題になっている技術・ガジェット系のホットエントリを取得する。

Google Trends は日本のRSS仕様変更が多いので使用せず、
安定して取れる4ソースから集める:
- はてなブックマーク テクノロジー hotentry
- Reddit r/gadgets
- Reddit r/technology
- Hacker News フロントページ
"""
import sys
from datetime import datetime, timedelta, timezone
import feedparser
import requests


_TREND_FEEDS = [
    ('はてブ テクノロジー', 'https://b.hatena.ne.jp/hotentry/it.rss'),
    ('Reddit r/gadgets', 'https://www.reddit.com/r/gadgets/.rss'),
    ('Reddit r/technology', 'https://www.reddit.com/r/technology/.rss'),
    ('Hacker News', 'https://hnrss.org/frontpage'),
    ('Reddit r/apple', 'https://www.reddit.com/r/apple/.rss'),
    ('Reddit r/Android', 'https://www.reddit.com/r/Android/.rss'),
]

_HTTP_TIMEOUT = 12
_USER_AGENT = 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)'


def fetch_trends(per_source=8, hours=72):
    """急上昇トピックを集めて返す。

    戻り値: [{'source', 'title', 'url', 'summary', 'published'}, ...]
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for source_name, url in _TREND_FEEDS:
        try:
            r = requests.get(
                url,
                timeout=_HTTP_TIMEOUT,
                headers={'User-Agent': _USER_AGENT, 'Accept': 'application/rss+xml,application/xml,*/*'},
            )
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for entry in feed.entries[:per_source]:
                published = _parse_published(entry)
                if published and published < cutoff:
                    continue
                summary = (entry.get('summary', '') or '').strip()
                # HTMLタグを雑に除去
                import re
                summary = re.sub(r'<[^>]+>', '', summary)[:240]
                items.append({
                    'source': source_name,
                    'title': (entry.get('title', '') or '').strip(),
                    'url': entry.get('link', ''),
                    'summary': summary,
                    'published': published,
                })
            print(f'[trends] {source_name}: {per_source} items', flush=True)
        except Exception as e:
            print(f'[trends] ⚠ {source_name} failed: {e}', file=sys.stderr, flush=True)

    items.sort(
        key=lambda x: x['published'] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


def _parse_published(entry):
    for key in ('published_parsed', 'updated_parsed'):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None
