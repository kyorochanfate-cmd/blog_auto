"""既存の Amazon/楽天 テキストアフィリリンクを最新の商品カード形式に置換する。

旧記事は `[商品名 を Amazon で探す](url) / [楽天で探す](url)` のテキスト形式で
リンクが入っているが、これを `[PRODUCT_CARD: 商品名]` プレースホルダに変換し、
`product_search.replace_placeholders` で実商品カード HTML に展開する。

使い方:
  body_new, info = upgrade_body(body_markdown, blog)
  # body_new = カード化済み markdown
  # info = {'cards_inserted': int, 'amazon_only': int, 'kept': int}
"""
import re
import requests
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from src import product_search


# ===== 旧アフィリリンクのパターン =====

# パターン1: 「商品名 を Amazon で探す / 楽天で探す」 (フル形式)
_FULL_PATTERN = re.compile(
    r'\[([^\]]+?)\s*を\s*Amazon\s*で探す\]\([^)]+\)'
    r'\s*/\s*\[楽天で探す\]\([^)]+\)'
)

# パターン2: Amazon のみ
_AMAZON_ONLY_PATTERN = re.compile(
    r'\[([^\]]+?)\s*を\s*Amazon\s*で探す\]\([^)]+\)'
)


def _to_placeholders(body):
    """旧アフィリ表記を [PRODUCT_CARD: 商品名] に置換。

    Returns: (new_body, count_full, count_amazon_only)
    """
    full_count = [0]
    amazon_only_count = [0]

    def repl_full(m):
        name = m.group(1).strip()
        full_count[0] += 1
        return f'[PRODUCT_CARD: {name}]'

    def repl_amazon(m):
        name = m.group(1).strip()
        amazon_only_count[0] += 1
        return f'[PRODUCT_CARD: {name}]'

    # フル形式を先に処理 (Amazon-only パターンに食われないように)
    body = _FULL_PATTERN.sub(repl_full, body)
    body = _AMAZON_ONLY_PATTERN.sub(repl_amazon, body)
    return body, full_count[0], amazon_only_count[0]


def upgrade_body(body, blog):
    """記事本文の旧アフィリ表記をカード化する。

    Args:
        body: Markdown 本文
        blog: ブログ設定 dict (rakuten_app_id 等含む)

    Returns:
        (new_body, info) info = {'placeholders': int, 'cards_inserted': int}
    """
    body2, full, amazon_only = _to_placeholders(body)
    placeholders = full + amazon_only

    if placeholders == 0:
        return body, {'placeholders': 0, 'cards_inserted': 0,
                      'full': 0, 'amazon_only': 0}

    # カウント前にプレースホルダ数を確認
    before_card_count = body2.count('[PRODUCT_CARD:')

    body3 = product_search.replace_placeholders(body2, blog)
    after_remaining = body3.count('[PRODUCT_CARD:')
    cards_inserted = before_card_count - after_remaining

    return body3, {
        'placeholders': placeholders,
        'cards_inserted': cards_inserted,
        'full': full,
        'amazon_only': amazon_only,
    }


# ===== Hatena AtomPub: エントリ取得/更新 =====

_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom',
            'app': 'http://www.w3.org/2007/app',
            'hatena': 'http://www.hatena.ne.jp/info/xmlns#'}

_UPDATE_ENTRY_XML = """<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://www.w3.org/2005/Atom"
       xmlns:app="http://www.w3.org/2007/app">
  <title>{title}</title>
  <author><name>{author}</name></author>
  <content type="text/x-markdown">{content}</content>
{categories}  <updated>{updated}</updated>
  <app:control>
    <app:draft>no</app:draft>
  </app:control>
</entry>"""


def _list_entries(blog, page_url=None):
    """ブログのエントリ一覧を1ページ取得。next ページURLも返す。"""
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
        # edit リンク (PUT 用)
        edit_link = ''
        alt_link = ''
        for link in entry_el.findall('atom:link', _ATOM_NS):
            rel = link.get('rel', '')
            href = link.get('href', '')
            if rel == 'edit':
                edit_link = href
            elif rel == 'alternate':
                alt_link = href
        # draft フラグ
        draft = 'no'
        ctrl = entry_el.find('app:control', _ATOM_NS)
        if ctrl is not None:
            d = ctrl.find('app:draft', _ATOM_NS)
            if d is not None and d.text:
                draft = d.text.strip()
        entries.append({
            'title': title, 'edit_url': edit_link,
            'alternate_url': alt_link, 'draft': draft,
        })

    # 次ページ
    next_link = ''
    for link in root.findall('atom:link', _ATOM_NS):
        if link.get('rel') == 'next':
            next_link = link.get('href', '')
            break

    return entries, next_link


def find_entry_by_title(blog, title_substring, max_pages=30):
    """タイトル部分一致で公開済みエントリを探す。最初に一致した1件を返す。"""
    page_url = None
    seen = 0
    for _ in range(max_pages):
        entries, next_link = _list_entries(blog, page_url)
        seen += len(entries)
        for e in entries:
            if e['draft'] == 'yes':
                continue
            if title_substring.lower() in e['title'].lower():
                return e, seen
        if not next_link:
            break
        page_url = next_link
    return None, seen


def fetch_entry(blog, edit_url):
    """edit_url から1エントリの完全データを取得。
    Returns: {'title','content','categories':[...]}
    """
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']
    r = requests.get(edit_url, auth=(hatena_id, api_key), timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    title_el = root.find('atom:title', _ATOM_NS)
    content_el = root.find('atom:content', _ATOM_NS)
    title = (title_el.text or '') if title_el is not None else ''
    content = (content_el.text or '') if content_el is not None else ''
    categories = []
    for cat in root.findall('atom:category', _ATOM_NS):
        term = cat.get('term', '').strip()
        if term:
            categories.append(term)
    return {'title': title, 'content': content, 'categories': categories}


def update_entry(blog, edit_url, title, content, categories):
    """エントリを PUT で更新。"""
    hatena_id = blog['hatena_id']
    api_key = blog['hatena_api_key']

    from datetime import datetime, timezone
    updated = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    cats_xml = ''
    for c in categories:
        cats_xml += f'  <category term="{escape(c)}" />\n'

    payload = _UPDATE_ENTRY_XML.format(
        title=escape(title),
        author=escape(hatena_id),
        content=escape(content),
        categories=cats_xml,
        updated=updated,
    )

    r = requests.put(
        edit_url,
        data=payload.encode('utf-8'),
        auth=(hatena_id, api_key),
        headers={'Content-Type': 'application/xml; charset=utf-8'},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f'はてなAPI更新エラー (status={r.status_code}):\n{r.text[:500]}'
        )
    return True


def get_latest_published_entry(blog):
    """ブログの公開済み最新エントリ1件を取得 (タイトル/URL/本文抜粋付き)。"""
    entries, _ = _list_entries(blog)
    for e in entries:
        if e['draft'] == 'yes':
            continue
        full = fetch_entry(blog, e['edit_url'])
        # 本文から概要を作る (最初の200字)
        body = full.get('content', '') or ''
        # マークダウン記号と[:contents]を除去して短い要約を作る
        import re as _re
        summary = _re.sub(r'\[:contents\]', '', body)
        summary = _re.sub(r'^#+\s*', '', summary, flags=_re.M)
        summary = _re.sub(r'!\[[^\]]*\]\([^)]+\)', '', summary)  # 画像除去
        summary = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', summary)  # リンク
        summary = _re.sub(r'[*_>`#-]', '', summary)
        summary = _re.sub(r'\s+', ' ', summary).strip()
        return {
            'title': e['title'],
            'url': e['alternate_url'],
            'summary': summary[:300],
        }
    return None


def append_banner_to_latest(blog):
    """最新の公開記事にグループバナーを追加 (既に入っていれば何もしない)。
    pending publish 経由で出した記事のバナー漏れ救済用。
    """
    banners = (blog.get('hatena_group_banners') or '').strip()
    if not banners:
        return {'ok': False, 'error': 'no banners configured'}

    entries, _ = _list_entries(blog)
    for e in entries:
        if e['draft'] == 'yes':
            continue
        entry = fetch_entry(blog, e['edit_url'])
        body = entry['content'] or ''
        if banners in body:
            return {'ok': False, 'error': 'banner already present',
                    'title': entry['title'], 'url': e['alternate_url']}
        new_body = body.rstrip() + '\n\n' + banners + '\n'
        update_entry(blog, e['edit_url'], entry['title'], new_body, entry['categories'])
        return {'ok': True, 'title': entry['title'],
                'url': e['alternate_url'],
                'added_chars': len(new_body) - len(body)}
    return {'ok': False, 'error': 'no published entry'}


def rewrite_one_article_completely(blog, title_substring, dry_run=False, extra_source_text=''):
    """既存記事を「現在の品質基準」で完全リライトする (URL は維持して SEO momentum を保つ)。

    手順:
      1. Hatena から既存記事を取得
      2. 既存本文を「ソース材料」として Gemini に渡す
      3. 現在の ARTICLE_PROMPT (砕けた口調・AR/VR niche・カード必須など) で再生成
      4. PRODUCT_CARD プレースホルダを実商品カードに置換
      5. 同 URL で PUT (Hatena 側で内容更新、URL/公開日は維持)

    Returns: {ok, old_len, new_len, new_title, url}
    """
    from src.article_generator import generate_article
    from src import product_search

    entry_meta, scanned = find_entry_by_title(blog, title_substring)
    if not entry_meta:
        return {'ok': False, 'error': f'entry not found for "{title_substring}" (scanned={scanned})'}

    entry = fetch_entry(blog, entry_meta['edit_url'])
    old_title = entry['title']
    old_body = entry['content'] or ''

    # 既存記事を Gemini への「ソース材料」として整形
    sources = [{
        'title': old_title,
        'url': entry_meta['alternate_url'],
        'source': '自ブログ過去記事 (リライト元)',
        'text': old_body[:8000],  # ソース長制限
        'image': None,
    }]
    # 公式スペック等の追加コンテキストがあれば、第二のソースとして注入
    if extra_source_text:
        sources.append({
            'title': f'{old_title} の公式スペック・追加情報',
            'url': '',
            'source': '公式サイト/参考情報',
            'text': extra_source_text[:6000],
            'image': None,
        })

    # 記事タイトルから topic_name を推測 (商品名・トピック語をそのまま)
    topic_name = old_title
    topic_summary = (
        '過去に書いた記事を、現在のブログ品質基準 (AR/VR特化・砕けた口調・カード必須・8000字以上・盗用対策) で完全に書き直す。'
        '【絶対遵守】元記事に書かれている **価格・サイズ・重量・解像度・バッテリー容量・型番・対応規格・発売日** 等の具体数値・固有名詞は'
        '**ひとつも失わずに新記事に引き継ぐ**こと。これらはガジェット紹介ブログの生命線。'
        '構成・表現・切り口・比喩・段落順は全部独自に変えていいが、数値とスペックは必ず本文に残す。'
        '"ベネフィット翻訳"はスペックを消すことではない。スペック+生活翻訳の併記が正しい形。'
    )

    # 楽天API資格情報チェック
    use_product_cards = bool(
        (blog.get('rakuten_app_id') or '').strip()
        and (blog.get('rakuten_access_key') or '').strip()
    )

    try:
        new_title, new_body = generate_article(
            topic_name, topic_summary, sources,
            tone_prompt=blog.get('tone_prompt', ''),
            genre=blog.get('genre') or 'AR/VR・空間コンピューティング',
            amazon_affiliate_tag=blog.get('amazon_affiliate_tag', ''),
            rakuten_affiliate_id=blog.get('rakuten_affiliate_id', ''),
            article_policy=blog.get('article_policy', ''),
            use_charts=False,  # 既存記事リライトでは Chart 不要 (Gemini が変数捏造する恐れ)
            wiki_images=None,
            official_image=None,
            ownership='not_owned',
            related_articles=None,
            longtail_keywords=None,
            use_product_cards=use_product_cards,
        )
    except Exception as e:
        return {'ok': False, 'error': f'generate failed: {e}'}

    # PRODUCT_CARD プレースホルダを実商品カードに変換
    if use_product_cards:
        try:
            new_body = product_search.replace_placeholders(new_body, blog)
        except Exception as e:
            print(f'[rewrite] product card replace failed: {e}', flush=True)

    if dry_run:
        return {
            'ok': True, 'dry_run': True,
            'old_title': old_title, 'new_title': new_title,
            'old_len': len(old_body), 'new_len': len(new_body),
            'url': entry_meta['alternate_url'],
            'new_body_preview': new_body[:1500],
        }

    # Hatena に更新 (URL は同じ)
    try:
        update_entry(blog, entry_meta['edit_url'], new_title, new_body, entry.get('categories', []))
    except Exception as e:
        return {'ok': False, 'error': f'update failed: {e}'}

    # Google Indexing API で再クロール促進
    try:
        from src import google_indexing
        google_indexing.notify_url(entry_meta['alternate_url'])
    except Exception:
        pass

    return {
        'ok': True,
        'old_title': old_title, 'new_title': new_title,
        'old_len': len(old_body), 'new_len': len(new_body),
        'url': entry_meta['alternate_url'],
    }


def upgrade_one_article(blog, title_substring):
    """1記事を検索 → アフィリ表記をカード化 → 更新。

    Returns: dict 結果サマリ
    """
    entry_meta, scanned = find_entry_by_title(blog, title_substring)
    if not entry_meta:
        return {'ok': False, 'error': f'記事が見つかりません (scanned={scanned})'}

    entry = fetch_entry(blog, entry_meta['edit_url'])
    old_body = entry['content']

    new_body, info = upgrade_body(old_body, blog)

    if info['cards_inserted'] == 0:
        return {
            'ok': False,
            'error': '変換対象のアフィリエイトリンクが見つかりませんでした',
            'title': entry['title'],
            'placeholders': info['placeholders'],
            'alternate_url': entry_meta['alternate_url'],
        }

    update_entry(blog, entry_meta['edit_url'], entry['title'], new_body, entry['categories'])

    return {
        'ok': True,
        'title': entry['title'],
        'alternate_url': entry_meta['alternate_url'],
        'old_length': len(old_body),
        'new_length': len(new_body),
        **info,
    }
