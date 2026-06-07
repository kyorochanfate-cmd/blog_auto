"""記事Markdown内の外部画像URLを Hatenaフォトライフへ再ホストする。

理由:
- Google News サムネ (lh3.googleusercontent.com) は数時間〜数日で消える
- ITmedia / Engadget はHatenaドメインからの hotlink を Referer で拒否
- メーカー公式画像は Bot 検知で 403

→ 一度ダウンロードしてHatenaに再アップロードすれば全部解決。コストゼロ。
"""
import re
from urllib.parse import urlparse
import requests

from src.hatena_publisher import upload_photo


_IMG_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')

_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

# 既に安定なホスト → 再ホスト不要
_SKIP_HOSTS = (
    'st-hatena.com',
    'hatena.ne.jp',
    'hatenablog.com',
    'hatena-fotolife.com',
    'quickchart.io',  # QuickChartの動的画像 (再アップしてもconfigが消えない)
)

_CONTENT_TYPE_TO_EXT = {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/heic': '.heic',
}

_MAX_BYTES = 10 * 1024 * 1024  # 10MB上限 (upload_photoと合わせる)


def _should_skip(url):
    if not url or not url.startswith('http'):
        return True
    if url.startswith('data:'):
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return True
    return any(s in host for s in _SKIP_HOSTS)


def _safe_filename(alt, ext):
    base = (alt or 'image').strip()[:30]
    base = re.sub(r'[\\/:*?"<>|]', '_', base) or 'image'
    return base + ext


def _try_rehost(blog, url, alt):
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                'User-Agent': _BROWSER_UA,
                'Accept': 'image/*,*/*;q=0.8',
                'Accept-Language': 'ja,en;q=0.9',
            },
            allow_redirects=True,
            stream=True,
        )
    except Exception as e:
        print(f'[rehost] download failed: {e} ({url})', flush=True)
        return None

    if r.status_code >= 400:
        print(f'[rehost] HTTP {r.status_code} ({url})', flush=True)
        return None

    content_type = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    if not content_type.startswith('image/'):
        print(f'[rehost] not image (CT={content_type}) ({url})', flush=True)
        return None

    try:
        data = r.content
    except Exception as e:
        print(f'[rehost] read body failed: {e} ({url})', flush=True)
        return None

    if not data or len(data) > _MAX_BYTES:
        print(f'[rehost] bad size ({len(data) if data else 0} bytes) ({url})', flush=True)
        return None

    ext = _CONTENT_TYPE_TO_EXT.get(content_type, '.jpg')
    filename = _safe_filename(alt, ext)

    try:
        new_url = upload_photo(blog, data, filename)
    except Exception as e:
        print(f'[rehost] upload failed: {e}', flush=True)
        return None

    print(f'[rehost] OK {url} -> {new_url}', flush=True)
    return new_url


def _remove_image_with_citation(markdown, img_start, img_end):
    """画像とその直後の引用行を1セットで削除し、過剰な空行をたたむ。"""
    line_start = markdown.rfind('\n', 0, img_start) + 1
    line_end = markdown.find('\n', img_end)
    if line_end == -1:
        line_end = len(markdown)

    # 直後の `> 引用元:` 行をチェック
    cursor = line_end + 1 if line_end < len(markdown) else line_end
    if cursor < len(markdown):
        next_nl = markdown.find('\n', cursor)
        if next_nl == -1:
            next_nl = len(markdown)
        next_line = markdown[cursor:next_nl].strip()
        if next_line.startswith('>') and any(
            kw in next_line for kw in ('引用', '出典', '画像:', '画像 :', '画像出典', 'credit', 'photo')
        ):
            line_end = next_nl

    new_md = markdown[:line_start] + (markdown[line_end + 1:] if line_end < len(markdown) else '')
    new_md = re.sub(r'\n{3,}', '\n\n', new_md)
    return new_md


def rehost_external_images(blog, markdown):
    """Markdown内の外部画像URLを Hatena Fotolifeへ移行する。

    戻り値: (new_markdown, stats)
      stats = {'rehosted': N, 'skipped': N, 'removed': N}
    冪等: 既にHatena URLならスキップする。
    """
    if not markdown:
        return markdown, {'rehosted': 0, 'skipped': 0, 'removed': 0}
    if not (blog.get('hatena_id') and blog.get('hatena_api_key')):
        return markdown, {'rehosted': 0, 'skipped': 0, 'removed': 0}

    matches = list(_IMG_PATTERN.finditer(markdown))
    if not matches:
        return markdown, {'rehosted': 0, 'skipped': 0, 'removed': 0}

    rehosted = 0
    skipped = 0
    removed = 0

    new_md = markdown
    # 後ろから処理することで position offset の調整不要
    for m in reversed(matches):
        alt = m.group(1)
        url = m.group(2)

        if _should_skip(url):
            skipped += 1
            continue

        new_url = _try_rehost(blog, url, alt)
        if new_url:
            replacement = f'![{alt}]({new_url})'
            new_md = new_md[:m.start()] + replacement + new_md[m.end():]
            rehosted += 1
        else:
            new_md = _remove_image_with_citation(new_md, m.start(), m.end())
            removed += 1

    return new_md, {'rehosted': rehosted, 'skipped': skipped, 'removed': removed}
