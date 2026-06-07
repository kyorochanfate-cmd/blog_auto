"""記事末尾の「次に読むならこちら」CTA ブロック生成。

1記事 1セッション平均PVを 1.0 → 1.5+ に押し上げる目的。
Markdown のリンク箇条書きより、視覚的なカード型 CTA の方がクリックされやすい。

インラインスタイルで実装(はてなブログのデザインCSSは触らない)。
"""
import re
from html import escape


def _html_escape(s):
    if not s:
        return ''
    return escape(s, quote=True)


def build_cta_block(related_articles, hub_article=None, max_cards=3):
    """関連記事リストから CTA ブロック HTML を組み立てる。

    Args:
        related_articles: [{'title','url'}, ...]
        hub_article: {'title','url'} or None (該当ジャンルのハブ記事)
        max_cards: 表示する関連記事カード数

    Returns: HTML 文字列 (Markdown 内に直接埋め込める)
    """
    if not related_articles and not hub_article:
        return ''

    s_box = (
        'margin:40px 0 24px 0;padding:24px 20px;'
        'border:1px solid #e0e0e0;border-radius:12px;'
        'background:linear-gradient(180deg,#fbfbfd 0%,#f5f6fa 100%);'
        'box-sizing:border-box;'
    )
    s_title = (
        'margin:0 0 16px 0;font-size:18px;font-weight:700;'
        'color:#222;border-bottom:2px solid #4e79a7;padding-bottom:8px;'
        'display:flex;align-items:center;gap:6px;'
    )
    s_card_link = (
        'display:block;padding:14px 16px;margin-bottom:10px;'
        'background:#fff;border-radius:8px;'
        'text-decoration:none !important;'
        'border-left:4px solid {accent};'
        'box-shadow:0 1px 3px rgba(0,0,0,0.04);'
        'transition:transform 0.1s ease;'
    )
    s_card_title = (
        'display:block;font-weight:700;font-size:15px;line-height:1.45;'
        'color:#222;margin-bottom:4px;'
    )
    s_card_cta = (
        'display:block;font-size:12px;color:#666;'
    )
    s_hub_box = (
        'margin-top:12px;padding:16px;'
        'background:#fff5e6;border:1px solid #f5b860;border-radius:8px;'
        'text-align:center;'
    )
    s_hub_link = (
        'display:inline-block;padding:8px 20px;'
        'background:#ff8c1a;color:#fff !important;'
        'text-decoration:none !important;border-radius:6px;'
        'font-weight:700;font-size:14px;'
    )
    s_hub_label = 'font-size:12px;color:#995500;margin-bottom:6px;display:block;'

    parts = [f'<div style="{s_box}">']
    parts.append(f'<h3 style="{s_title}">次に読むならこちら</h3>')

    accents = ['#4e79a7', '#e15759', '#59a14f', '#f28e2b', '#76b7b2']
    for i, a in enumerate((related_articles or [])[:max_cards]):
        title = _html_escape((a.get('title') or '').strip())
        url = _html_escape((a.get('url') or '').strip())
        if not (title and url):
            continue
        accent = accents[i % len(accents)]
        link_style = s_card_link.format(accent=accent)
        parts.append(
            f'<a href="{url}" style="{link_style}">'
            f'<span style="{s_card_title}">{title}</span>'
            f'<span style="{s_card_cta}">関連記事を読む →</span>'
            f'</a>'
        )

    if hub_article:
        hub_title = _html_escape((hub_article.get('title') or '').strip())
        hub_url = _html_escape((hub_article.get('url') or '').strip())
        if hub_title and hub_url:
            parts.append(
                f'<div style="{s_hub_box}">'
                f'<span style="{s_hub_label}">このジャンルを総合的に読む</span>'
                f'<a href="{hub_url}" style="{s_hub_link}">'
                f'{hub_title} →</a>'
                f'</div>'
            )

    parts.append('</div>')
    return '\n\n' + ''.join(parts) + '\n\n'


_RELATED_SECTION_RE = re.compile(r'\n##\s*関連記事\b', re.MULTILINE)


def _is_url_alive(url):
    """URL が 2xx/3xx を返すか確認。失敗・404 等は False。HEAD で軽量チェック。"""
    if not url:
        return False
    try:
        import requests
        r = requests.head(url, timeout=8, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0 (BlogChecker)'})
        # HEAD が 404 でも GET なら 200 のサイトもあるので、404の時は GET 再確認
        if r.status_code == 404:
            g = requests.get(url, timeout=8, allow_redirects=True, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (BlogChecker)'})
            g.close()
            return 200 <= g.status_code < 400
        return 200 <= r.status_code < 400
    except Exception:
        return False


def inject_into_body(body, related_articles, hub_article=None):
    """記事本文の「## 関連記事」直前 or 末尾に CTA ブロックを差し込む。

    冪等: 既に挿入されていれば再挿入しない (重複防止)。
    URL 死リンクは挿入前に除去 (related_articles と hub_article のURLをHEADチェック)
    """
    if not body:
        return body
    if 'cta-block-marker' in body:
        return body  # 既に挿入済み

    # 死リンク事前チェック
    if related_articles:
        related_articles = [a for a in related_articles if _is_url_alive(a.get('url', ''))]
    if hub_article and not _is_url_alive(hub_article.get('url', '')):
        hub_article = None

    cta = build_cta_block(related_articles, hub_article)
    if not cta:
        return body

    # マーカーを HTML コメントで埋め込み (再挿入検知用)
    cta = cta.replace('<div style="', '<div data-tag="cta-block-marker" style="', 1)

    m = _RELATED_SECTION_RE.search(body)
    if m:
        return body[:m.start()] + cta + body[m.start():]
    return body.rstrip() + cta
