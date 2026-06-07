"""Wikimedia Commons image search — freely licensed images only."""
import json
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

_COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
_COMMERCIAL_OK = ['cc0', 'cc by 4.0', 'cc by 3.0', 'cc by 2.5', 'cc by 2.0', 'cc by 1.0',
                  'cc by-sa 4.0', 'cc by-sa 3.0', 'cc by-sa 2.5', 'cc by-sa 2.0', 'cc by-sa 1.0',
                  'public domain', 'pd-', 'public_domain', 'pdm']
_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
_HEADERS = {'User-Agent': 'BlogAutoTool/1.0 (kyorochan.fate@gmail.com)'}


def _is_commercially_usable(license_str):
    """CC BY-NC など非商用ライセンスを除外し、商用利用可能なもののみ通す。"""
    s = license_str.lower()
    # NC（非商用）を明示的に除外
    if 'nc' in s:
        return False
    return any(p in s for p in _COMMERCIAL_OK)


# 「記事の実体画像ではない」と判別される画像ホスト/パターン
_GARBAGE_IMAGE_HOSTS = (
    'lh3.googleusercontent.com',  # Google News アイコン
    'lh4.googleusercontent.com',
    'lh5.googleusercontent.com',
    'lh6.googleusercontent.com',
    'news.google.com',
    'ssl.gstatic.com',
)

_GARBAGE_PATH_PATTERNS = (
    '/logo.', '/favicon.', '/icon.',
    'google-news', 'googlenews',
    'placeholder', 'no-image', 'noimage', 'default-thumb',
    '/branding/',
)


def is_likely_garbage_image(url):
    """画像URLが「記事の中身を表していない可能性が高い」かを判定する。

    - Google News のアプリアイコン (lh3.googleusercontent.com 経由)
    - 一般的なロゴ/プレースホルダ
    - SVG (基本ロゴ)
    すでに Hatena Fotolife に再ホスト済みのものは常に正常扱い。
    """
    if not url:
        return True
    lower = url.lower()
    # Hatena 内部の画像は常に有効
    if 'st-hatena.com' in lower or 'hatena.ne.jp' in lower:
        return False
    if any(h in lower for h in _GARBAGE_IMAGE_HOSTS):
        return True
    if any(p in lower for p in _GARBAGE_PATH_PATTERNS):
        return True
    if lower.endswith('.svg') or '.svg?' in lower:
        return True
    return False


def _strip_html(html):
    return re.sub(r'<[^>]+>', '', html or '').strip()


_WIKI_SEARCH_TERMS_PROMPT = """以下の記事トピックを Wikimedia Commons で画像検索するとき、最もヒットしやすい「検索ワード」を 1〜3個 提案してください。

【トピック名】{topic_name}

【概要】{topic_summary}

【検索ワードの条件】
- Wikipedia / Wikimedia Commons に項目がありそうな **具体的・代表的な固有名詞** を選ぶ
- 製品名・メーカー名・人物名・サービス名・技術名・場所名など
- 抽象概念のみ(「ガバナンス」「動向」「戦略」等)は避ける、その場合は分野そのもの(AI / Smartphone 等)に置き換える
- 英語と日本語のどちらでもOK(Wikimedia は多言語対応)。**英語の方がヒット率高い**ので優先
- 例:
  - 「AIエージェントの暴走とガバナンス」 → ["artificial intelligence", "AI"]
  - 「iPhone 17 のリーク情報」 → ["iPhone", "Apple Inc."]
  - 「Anker 新型充電器のセール」 → ["Anker", "USB-C charger"]
  - 「Switch 2 周辺機器まとめ」 → ["Nintendo Switch", "video game console"]

【出力形式】JSON のみ。前置き・コードフェンス禁止。
{{"terms": ["検索ワード1", "検索ワード2"]}}
"""


def _extract_wiki_search_terms(topic_name, topic_summary=''):
    """Wikimedia 検索向けキーワードを Gemini に抽出させる。失敗時は topic_name のみ。"""
    if not topic_name:
        return []
    prompt = _WIKI_SEARCH_TERMS_PROMPT.format(
        topic_name=(topic_name or '').strip(),
        topic_summary=(topic_summary or '').strip(),
    )
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        print(f'[wiki-terms] gemini failed: {e}', flush=True)
        return [topic_name]

    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            return [topic_name]
        try:
            data = json.loads(m.group(0))
        except Exception:
            return [topic_name]

    terms = [str(t).strip() for t in (data.get('terms') or []) if str(t).strip()]
    if not terms:
        return [topic_name]
    return terms[:3]


def find_images_smart(topic_name, topic_summary='', count=2):
    """Wikimedia Commons でトピックに合う画像を探す賢い版。

    Gemini で検索キーワードを抽出 → 候補を順に試行 → 最初にヒットしたものを返す。
    全部空振りなら topic_name そのままでも再検索。それでも 0 件なら [] を返す。
    """
    terms = _extract_wiki_search_terms(topic_name, topic_summary)
    print(f'[wiki-smart] terms for "{(topic_name or "")[:40]}": {terms}', flush=True)

    for term in terms:
        imgs = find_images(term, count=count)
        if imgs:
            print(f'[wiki-smart] hit on "{term}": {len(imgs)} images', flush=True)
            return imgs

    # Geminiの提案が全滅した時の最後の保険: topic_name 直叩き
    if topic_name and topic_name not in terms:
        imgs = find_images(topic_name, count=count)
        if imgs:
            print(f'[wiki-smart] hit on topic_name fallback', flush=True)
            return imgs

    print(f'[wiki-smart] no hit for any term', flush=True)
    return []


def find_images(query, count=2):
    """Return up to `count` freely-licensed images from Wikimedia Commons."""
    try:
        resp = requests.get(_COMMONS_API, params={
            'action': 'query',
            'generator': 'search',
            'gsrnamespace': 6,
            'gsrsearch': query,
            'gsrlimit': count * 5,
            'prop': 'imageinfo',
            'iiprop': 'url|extmetadata',
            'iiurlwidth': 800,
            'format': 'json',
        }, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        pages = resp.json().get('query', {}).get('pages', {}).values()
    except Exception:
        return []

    results = []
    for page in sorted(pages, key=lambda p: p.get('index', 999)):
        ii_list = page.get('imageinfo', [])
        if not ii_list:
            continue
        ii = ii_list[0]
        url = ii.get('thumburl') or ii.get('url', '')
        # Wikimedia は最近 ?utm_source=... を URL末尾に付けるようになったので
        # 拡張子チェックはクエリ文字列を除去してから行う
        url_no_query = url.split('?')[0].lower() if url else ''
        if not url_no_query or not any(url_no_query.endswith(ext) for ext in _IMAGE_EXTS):
            continue

        meta = ii.get('extmetadata', {})
        license_name = meta.get('LicenseShortName', {}).get('value', '')
        if not _is_commercially_usable(license_name):
            continue

        artist = _strip_html(meta.get('Artist', {}).get('value', '')) or 'Wikimedia Commons'
        page_title = page.get('title', '').replace(' ', '_')

        results.append({
            'url': url,
            'credit': artist,
            'license': license_name or 'CC',
            'page_url': f'https://commons.wikimedia.org/wiki/{page_title}',
        })
        if len(results) >= count:
            break

    return results


_OFFICIAL_PROMPT = """以下の製品/サービスについて、メーカー公式サイトの製品紹介ページURLを返してください。

製品名: {product}

ルール:
- 製造元/販売元の公式サイトのみ。Amazon・楽天・価格.com・ニュースサイト等は不可。
- ある程度の確信があれば候補を出してOK。複数候補があれば配列に複数入れて良い (最大3つ)。
- 全く分からない場合のみ "NONE" を返す。
- 出力は厳密に下記JSONのみ (前置き・コードフェンス不要):
{{"candidates": [{{"url": "https://...", "maker": "メーカー名"}}, ...]}}
または {{"candidates": []}} (公式URLが分からない場合)
"""


_BLOCKED_HOST_HINTS = (
    'amazon.', 'rakuten.', 'kakaku.com', 'yahoo.', 'mercari.', 'au.com',
    'docomo.', 'softbank.', 'biccamera.', 'yodobashi.', 'joshin.',
    'news.', 'itmedia.', 'ascii.', 'impress.', 'gizmodo.', 'engadget.',
    'gigazine.', 'getnews.', 'cnet.', 'theverge.', '4gamer.', 'famitsu.',
    'wikipedia.', 'wikimedia.', 'youtube.com', 'youtu.be', 'twitter.',
    'x.com', 'facebook.', 'instagram.', 'reddit.', 'note.com', 'qiita.',
    'github.', 'medium.com', 'note.', 'hatena.ne.jp', 'hatenablog.',
    'google.com/search', 'news.google',
)


_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)


def _looks_official(url):
    """通販・ニュース・SNS等を除外し、それ以外は採用候補とする (ブラックリスト方式)。"""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    return not any(hint in host for hint in _BLOCKED_HOST_HINTS)


def _extract_og_image(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    for prop in ('og:image', 'twitter:image', 'og:image:url', 'og:image:secure_url'):
        tag = (soup.find('meta', attrs={'property': prop})
               or soup.find('meta', attrs={'name': prop}))
        if tag and tag.get('content'):
            url = tag['content'].strip()
            if url.startswith('//'):
                return 'https:' + url
            if url.startswith('/'):
                p = urlparse(base_url)
                return f'{p.scheme}://{p.netloc}{url}'
            return url
    return None


def find_official_image_from_sources(sources, product_name=''):
    """既に取得済みのソース記事から、メーカーCDN/メーカー公式由来の画像を探す。

    sources: [{url, image, title, source, text}, ...]
    各sourceは researcher.fetch_article_texts() で og:image を image に格納済み。
    """
    if not sources:
        return None

    # ソース記事URLが「公式っぽい」(ニュース・通販でない) ものを優先
    for s in sources:
        page_url = s.get('url', '')
        img = s.get('image')
        if not page_url or not img or is_likely_garbage_image(img):
            continue
        if _looks_official(page_url):
            print(f'[official-img] from-source(page-official) {page_url} -> {img}', flush=True)
            return {
                'url': img,
                'page_url': page_url,
                'maker': urlparse(page_url).netloc,
                'is_press_photo': True,
            }

    # それ以外でも、og:imageのホストがメーカーCDNなら採用
    for s in sources:
        img = s.get('image') or ''
        page_url = s.get('url', '')
        if not img or is_likely_garbage_image(img):
            continue
        if _looks_official(img):
            print(f'[official-img] from-source(img-official) {img} via {page_url}', flush=True)
            return {
                'url': img,
                'page_url': page_url or img,
                'maker': urlparse(img).netloc,
                'is_press_photo': True,
            }

    # 最終フォールバック: 最初のソースのog:imageをニュース引用として返す
    for s in sources:
        img = s.get('image')
        page_url = s.get('url', '')
        source_name = s.get('source', '')
        if img and page_url and not is_likely_garbage_image(img):
            print(f'[official-img] from-source(news-fallback) {page_url} -> {img}', flush=True)
            return {
                'url': img,
                'page_url': page_url,
                'maker': source_name or urlparse(page_url).netloc,
                'is_press_photo': False,
            }

    print('[official-img] no usable image in sources (all garbage or empty)', flush=True)
    return None


def _try_fetch_og(url):
    try:
        r = requests.get(
            url,
            timeout=12,
            headers={
                'User-Agent': _BROWSER_UA,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ja,en;q=0.9',
            },
            allow_redirects=True,
        )
    except Exception as e:
        print(f'[official-img] fetch error: {e} ({url})', flush=True)
        return None, None
    if r.status_code >= 400:
        print(f'[official-img] HTTP {r.status_code} {url}', flush=True)
        return None, r.url
    og = _extract_og_image(r.text, r.url)
    if og:
        print(f'[official-img] og:image found {r.url} -> {og}', flush=True)
    else:
        print(f'[official-img] no og:image on {r.url}', flush=True)
    return og, r.url


def find_official_image(product_name):
    """Geminiに公式URL候補を聞き、og:imageを抽出する。複数候補を順に試す。"""
    if not product_name or not product_name.strip():
        return None

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_OFFICIAL_PROMPT.format(product=product_name.strip()),
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = (resp.text or '').strip()
        if text.startswith('```'):
            lines = text.splitlines()
            text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])
        data = json.loads(text)
    except Exception as e:
        print(f'[official-img] gemini failed: {e}', flush=True)
        return None

    candidates = data.get('candidates') or []
    if not candidates:
        print(f'[official-img] gemini returned no candidates for "{product_name}"', flush=True)
        return None

    for cand in candidates[:3]:
        url = (cand.get('url') or '').strip()
        if not url or url == 'NONE' or not url.startswith('http'):
            continue
        if not _looks_official(url):
            print(f'[official-img] skipped (blocked host) {url}', flush=True)
            continue
        og, final_url = _try_fetch_og(url)
        if not og:
            continue
        if is_likely_garbage_image(og):
            print(f'[official-img] gemini-cand garbage og: {og[:80]}', flush=True)
            continue
        return {
            'url': og,
            'page_url': final_url or url,
            'maker': (cand.get('maker') or '').strip() or urlparse(url).netloc,
            'is_press_photo': True,
        }

    print(f'[official-img] all gemini candidates failed for "{product_name}"', flush=True)
    return None


def resolve_official_image(product_name, sources=None):
    """画像を探す: メーカー公式 og:image を最優先、見つからなければソース画像にフォールバック。

    フォールバック順:
      1. Gemini が見つけたメーカー公式サイトの og:image (理想)
      2. ソース記事の og:image (最終手段 — 「画像なし記事」を出さないため)

    Wikimedia 画像は別枠で `use_wiki_images=True` のときに個別注入される。
    sources が渡されていれば段階2を試す。
    """
    img = find_official_image(product_name)
    if img:
        return img
    if sources:
        print('[official-img] fallback to source media image', flush=True)
        return find_official_image_from_sources(sources, product_name=product_name)
    return None
