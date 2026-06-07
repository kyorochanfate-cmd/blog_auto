import sys
import trafilatura
import requests
from bs4 import BeautifulSoup


HTTP_TIMEOUT = 12
USER_AGENT = 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)'


def fetch_article_texts(items, max_items=3, per_source_chars=2500):
    sources = []
    for item in items[:max_items]:
        original_url = item.get('link', '')
        if not original_url:
            continue

        actual_url = _resolve_url(original_url)
        text = None
        image = None
        html = None

        try:
            html = trafilatura.fetch_url(actual_url)
            if html:
                text = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=False,
                )
                image = _extract_og_image(html)
                if text:
                    text = text[:per_source_chars]
                    print(f'[researcher] OK ({len(text)}文字) {actual_url}', flush=True)
                else:
                    print(f'[researcher] extract empty {actual_url}', flush=True)
            else:
                print(f'[researcher] fetch_url returned None {actual_url}', flush=True)
        except Exception as e:
            print(f'[researcher] fetch error: {e} ({actual_url})', file=sys.stderr, flush=True)

        if not text:
            # Fallback: use RSS title + summary so Gemini has at least something to work with.
            fallback_parts = [item.get('title') or '', item.get('summary') or '']
            fallback = '\n'.join(p for p in fallback_parts if p).strip()
            if fallback:
                text = fallback[:per_source_chars]
                print(f'[researcher] fallback to RSS summary ({len(text)}文字)', flush=True)

        if text:
            sources.append({
                'title': item.get('title', ''),
                'url': actual_url or original_url,
                'source': item.get('source', ''),
                'text': text,
                'image': image or item.get('thumbnail'),
            })

    print(f'[researcher] total sources: {len(sources)}', flush=True)
    return sources


def _resolve_url(url):
    """Resolve Google News redirect URLs to the actual article URL when possible."""
    if 'news.google.com' not in url:
        return url
    try:
        r = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers={'User-Agent': USER_AGENT},
            allow_redirects=True,
        )
        # HTTP redirect may have already taken us to the article
        if r.url and 'news.google.com' not in r.url:
            print(f'[researcher] resolved (http-redirect) -> {r.url}', flush=True)
            return r.url

        # Otherwise, parse the response for canonical/og:url
        soup = BeautifulSoup(r.text, 'html.parser')
        for finder in (
            lambda: (soup.find('link', rel='canonical') or {}).get('href'),
            lambda: (soup.find('meta', property='og:url') or {}).get('content'),
            lambda: _meta_refresh_target(soup),
            lambda: _first_external_link(soup),
        ):
            try:
                target = finder()
            except Exception:
                target = None
            if target and 'news.google.com' not in target and target.startswith('http'):
                print(f'[researcher] resolved (page-parse) -> {target}', flush=True)
                return target
    except Exception as e:
        print(f'[researcher] url resolution failed: {e} ({url})', file=sys.stderr, flush=True)
    return url


def _meta_refresh_target(soup):
    meta = soup.find('meta', attrs={'http-equiv': 'refresh'})
    if not meta:
        return None
    content = meta.get('content') or ''
    lower = content.lower()
    if 'url=' not in lower:
        return None
    return content[lower.index('url=') + 4:].split(';')[0].strip().strip('"\'')


def _first_external_link(soup):
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('http') and 'google.com' not in href:
            return href
    return None


def _extract_og_image(html):
    if not html:
        return None
    try:
        from src.image_finder import is_likely_garbage_image
    except Exception:
        is_likely_garbage_image = lambda u: False  # noqa
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for prop in ('og:image', 'twitter:image'):
            tag = (soup.find('meta', attrs={'property': prop})
                   or soup.find('meta', attrs={'name': prop}))
            if tag and tag.get('content'):
                url = tag['content'].strip()
                # Google News のロゴ等を弾く
                if is_likely_garbage_image(url):
                    print(f'[researcher] skip garbage og:image: {url[:80]}', flush=True)
                    continue
                return url
    except Exception:
        pass
    return None
