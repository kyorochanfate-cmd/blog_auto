"""記事本文中の [PRODUCT_CARD: 商品名] の直前に、
公式サイトの og:image を引用フィガーとして挿入する。

src/image_finder.find_official_image() を再利用:
  Gemini に「この商品のメーカー公式サイトURLは？」と聞き、
  返ってきた候補ページから og:image を抽出。
  公式ドメイン以外は弾く (_looks_official フィルタ)。

引用元は <figcaption> に明記し、リンクを貼ることで著作権法32条
（引用）の要件を満たす形にする。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from src import image_finder  # type: ignore
except Exception as _e:
    image_finder = None
    logging.getLogger(__name__).warning('image_finder unavailable: %s', _e)

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r'\[PRODUCT_CARD:\s*([^\]\n]+?)\s*\]')


def _build_figure_html(img_url: str, page_url: str, maker: str, product_name: str) -> str:
    alt = escape(product_name)
    cap_maker = escape(maker or '公式サイト')
    cap_page = escape(page_url)
    return (
        '<figure style="margin:1.2em 0;text-align:center;">'
        f'<img src="{escape(img_url)}" alt="{alt}" '
        'style="max-width:100%;height:auto;border:1px solid #eee;border-radius:4px;" />'
        '<figcaption style="font-size:0.85em;color:#666;margin-top:0.4em;">'
        f'出典: <a href="{cap_page}" rel="nofollow noopener" target="_blank">{cap_maker}</a>'
        '</figcaption>'
        '</figure>'
    )


def insert_official_images(markdown_body: str) -> str:
    """各 [PRODUCT_CARD: ...] の直前に公式画像フィガーを挿入。

    - 同じ商品名が複数回登場する場合、最初の1回だけ挿入（重複防止）
    - 公式画像が見つからなければ何もしない（PRODUCT_CARD はそのまま残る）
    - USE_OFFICIAL_IMAGES=0 で機能オフ
    """
    if image_finder is None:
        return markdown_body
    if os.environ.get('USE_OFFICIAL_IMAGES', '1') == '0':
        return markdown_body
    if '[PRODUCT_CARD:' not in markdown_body:
        return markdown_body

    matches = list(_PLACEHOLDER_RE.finditer(markdown_body))
    if not matches:
        return markdown_body

    seen: set[str] = set()
    inserted = 0
    out = markdown_body
    offset = 0
    for m in matches:
        kw = m.group(1).strip()
        key = kw.lower()
        if not kw or key in seen:
            continue
        seen.add(key)

        try:
            info = image_finder.find_official_image(kw)
        except Exception as e:
            log.warning('find_official_image error for "%s": %s', kw, e)
            continue
        if not info:
            log.info('[official-img] no image for "%s"', kw)
            continue

        figure = _build_figure_html(
            img_url=info.get('url', ''),
            page_url=info.get('page_url', ''),
            maker=info.get('maker', ''),
            product_name=kw,
        )
        insert_block = f'\n\n{figure}\n\n'
        pos = m.start() + offset
        out = out[:pos] + insert_block + out[pos:]
        offset += len(insert_block)
        inserted += 1
        log.info('[official-img] inserted for "%s"', kw)

    if inserted:
        log.info('Inserted %d official images', inserted)
    return out
