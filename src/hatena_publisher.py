import base64
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
import requests
from xml.sax.saxutils import escape


ENTRY_XML = """<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://www.w3.org/2005/Atom"
       xmlns:app="http://www.w3.org/2007/app">
  <title>{title}</title>
  <author><name>{author}</name></author>
  <content type="text/x-markdown">{content}</content>
{categories}  <app:control>
    <app:draft>{draft_value}</app:draft>
  </app:control>
</entry>"""


def publish(blog, title, markdown_body, extra_categories=None, draft=True):
    """
    blog: dict with hatena_id, hatena_api_key, hatena_blog_domain, hatena_category(optional)
    extra_categories: 追加で付与するカテゴリのリスト (Gemini で推測したもの等)
    draft: True なら下書き保存 (公開しない、ユーザーが Hatena 管理画面で手動公開)。デフォルト True (安全側)
    """
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']
    domain = blog['hatena_blog_domain']
    base_category = (blog.get('hatena_category') or '').strip()

    endpoint = f'https://blog.hatena.ne.jp/{hatena_id}/{domain}/atom/entry'

    # ベースカテゴリ + extra をマージして重複除去
    all_cats = []
    seen = set()
    if base_category:
        all_cats.append(base_category)
        seen.add(base_category.lower())
    for c in (extra_categories or []):
        c = (c or '').strip()
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        all_cats.append(c)

    cats_xml = ''
    for c in all_cats:
        cats_xml += f'  <category term="{escape(c)}" />\n'

    payload = ENTRY_XML.format(
        title=escape(title),
        author=escape(hatena_id),
        content=escape(markdown_body),
        categories=cats_xml,
        draft_value='yes' if draft else 'no',
    )

    r = requests.post(
        endpoint,
        data=payload.encode('utf-8'),
        auth=(hatena_id, api_key),
        headers={'Content-Type': 'application/xml; charset=utf-8'},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f'はてなAPIエラー (status={r.status_code}):\n{r.text[:500]}'
        )

    m = re.search(
        r'<link[^>]+rel="alternate"[^>]+href="([^"]+)"',
        r.text,
    )
    return m.group(1) if m else '(投稿は成功しましたが記事URLを取得できませんでした)'


_FOTOLIFE_ENDPOINT = 'https://f.hatena.ne.jp/atom/post'

_PHOTO_ENTRY_XML = """<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://purl.org/atom/ns#">
  <title>{title}</title>
  <content mode="base64" type="{mime}">{data}</content>
</entry>"""


def _wsse_header(username, password):
    nonce_bytes = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    digest = hashlib.sha1(nonce_bytes + created.encode('utf-8') + password.encode('utf-8')).digest()
    return (
        f'UsernameToken Username="{username}", '
        f'PasswordDigest="{base64.b64encode(digest).decode()}", '
        f'Nonce="{base64.b64encode(nonce_bytes).decode()}", '
        f'Created="{created}"'
    )


_MIME_BY_EXT = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.heic': 'image/heic',
}


def upload_photo(blog, file_bytes, filename):
    """はてなフォトライフへ画像をアップロードし、画像URLを返す。

    blog: dict with hatena_id, hatena_api_key
    file_bytes: 画像ファイルのバイト列
    filename: 元のファイル名（拡張子からMIMEを推測）
    """
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']

    ext = os.path.splitext(filename or '')[1].lower()
    mime = _MIME_BY_EXT.get(ext, 'image/jpeg')

    title = os.path.splitext(os.path.basename(filename or 'image'))[0] or 'image'

    payload = _PHOTO_ENTRY_XML.format(
        title=escape(title),
        mime=mime,
        data=base64.b64encode(file_bytes).decode('ascii'),
    )

    r = requests.post(
        _FOTOLIFE_ENDPOINT,
        data=payload.encode('utf-8'),
        headers={
            'Content-Type': 'application/xml; charset=utf-8',
            'X-WSSE': _wsse_header(hatena_id, api_key),
        },
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f'はてなフォトライフAPIエラー (status={r.status_code}):\n{r.text[:500]}'
        )

    m = re.search(r'<hatena:imageurl[^>]*>([^<]+)</hatena:imageurl>', r.text)
    if m:
        return m.group(1).strip()
    m = re.search(r'<content[^>]+src="([^"]+)"', r.text)
    if m:
        return m.group(1).strip()
    raise RuntimeError(f'画像URLを応答から抽出できませんでした:\n{r.text[:500]}')
