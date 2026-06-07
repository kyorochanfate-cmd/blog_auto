import sys
from datetime import datetime, timedelta, timezone
import feedparser
import requests


HTTP_TIMEOUT = 15
USER_AGENT = 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)'


def collect_recent_items(feeds, hours=72):
    """
    feeds: list of (source_name, url) tuples.
    Returns list of dicts with title/summary/link/published/thumbnail/source.

    Google News RSS の場合は <source> 要素から実際のパブリッシャー名を抽出して
    source フィールドに設定する (例: "The Verge" "ITmedia") — auto_runner の
    foreign/japanese 判定で使う。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    is_google_news_feed = lambda url: 'news.google.com' in (url or '').lower()

    items = []
    for source_name, url in feeds:
        gn = is_google_news_feed(url)
        try:
            print(f'[news] fetching {source_name}: {url}', flush=True)
            r = requests.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={'User-Agent': USER_AGENT},
            )
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            count_before = len(items)
            for entry in feed.entries:
                published = _parse_published(entry)
                if published and published < cutoff:
                    continue

                # Google News の場合: <source> 要素から実パブリッシャーを取得
                effective_source = source_name
                if gn:
                    actual = _extract_actual_publisher(entry)
                    if actual:
                        effective_source = actual

                items.append({
                    'source': effective_source,
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', ''),
                    'link': entry.get('link', ''),
                    'published': published,
                    'thumbnail': _extract_thumbnail(entry),
                })
            print(f'[news]   -> {len(items) - count_before} items', flush=True)
        except Exception as e:
            print(f'[news] ⚠ {source_name} failed: {e}', file=sys.stderr, flush=True)

    items.sort(
        key=lambda x: x['published'] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    print(f'[news] total {len(items)} items collected', flush=True)
    return items


def _parse_published(entry):
    for key in ('published_parsed', 'updated_parsed'):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _extract_actual_publisher(entry):
    """Google News RSS の <source> 要素からパブリッシャー名を取得。

    feedparser は <source url="...">Title</source> を entry.source に dict
    として格納する (キー: 'href' / 'title' / 'value' などバリエーションあり)。
    取得失敗時は None。
    """
    src = entry.get('source')
    if not src:
        return None
    if isinstance(src, str) and src.strip():
        return src.strip()
    if isinstance(src, dict):
        # feedparser の標準形式
        for k in ('title', 'value', 'name'):
            v = src.get(k)
            if v and isinstance(v, str) and v.strip():
                return v.strip()
    # FeedParserDict (= dict サブクラス) でも上の dict 分岐で取れる想定
    return None


def _extract_thumbnail(entry):
    try:
        from src.image_finder import is_likely_garbage_image
    except Exception:
        is_likely_garbage_image = lambda u: False  # noqa

    candidates = []
    media_thumbs = entry.get('media_thumbnail') or []
    if media_thumbs and media_thumbs[0].get('url'):
        candidates.append(media_thumbs[0]['url'])
    media_contents = entry.get('media_content') or []
    for mc in media_contents:
        if mc.get('url'):
            candidates.append(mc['url'])
    enclosures = entry.get('enclosures') or []
    for enc in enclosures:
        t = enc.get('type', '') or ''
        if t.startswith('image/') and enc.get('href'):
            candidates.append(enc['href'])

    for url in candidates:
        if not is_likely_garbage_image(url):
            return url
    return None
