"""楽天市場 Item Search API で実商品を検索。

商品カードに使う「実在する商品の情報」(画像URL・商品名・価格・アフィリエイトURL)
を取得する。Gemini のハルシネーションを防止しつつ、視覚的なカード表示を可能にする。

認証:
- applicationId + accessKey + Origin ヘッダー(登録ドメイン) すべて必須
- API URL: https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401
- Rate limit: 1 req/sec
"""
import re
import time
from urllib.parse import quote
import requests


_API_URL = 'https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401'

# Origin ヘッダーは登録ドメイン(=ブログのドメイン)を入れる必要がある
_DEFAULT_ORIGIN = 'https://tako-karamaru.hatenablog.com'

# レート制限回避用 (1req/sec)
_last_call_ts = [0.0]
_MIN_INTERVAL = 1.2


def search(keyword, blog, hits=1, sort='-reviewCount'):
    """楽天市場で keyword を検索。最も評価の高い商品 hits 件を返す。

    Args:
        keyword: 商品名(例: "Sony WH-1000XM5")
        blog: ブログ設定 dict (rakuten_app_id / rakuten_access_key / rakuten_affiliate_id 必要)
        hits: 取得件数 (1 〜 30)
        sort: 並び順 ('-reviewCount' = レビュー多い順、'standard' = 楽天順)

    Returns:
        [{'name','price','image_url','affiliate_url','shop'}, ...] or [] if 失敗 / 0件
    """
    app_id = (blog.get('rakuten_app_id') or '').strip()
    access_key = (blog.get('rakuten_access_key') or '').strip()
    aff_id = (blog.get('rakuten_affiliate_id') or '').strip()
    if not (app_id and access_key and keyword):
        return []

    # Origin ヘッダーはブログドメインから
    domain = (blog.get('hatena_blog_domain') or '').strip()
    origin = f'https://{domain}' if domain else _DEFAULT_ORIGIN

    # レート制限保護
    elapsed = time.time() - _last_call_ts[0]
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    params = {
        'format': 'json',
        'keyword': keyword,
        'applicationId': app_id,
        'accessKey': access_key,
        'hits': max(1, min(int(hits), 30)),
        'sort': sort,
        'minPrice': 500,  # ジャンク・送料のみ商品を除外
        'imageFlag': 1,   # 画像付き商品のみ
    }
    if aff_id:
        params['affiliateId'] = aff_id

    try:
        r = requests.get(_API_URL, params=params,
                         headers={'Origin': origin}, timeout=12)
    except Exception as e:
        print(f'[product-search] request error: {e}', flush=True)
        return []
    finally:
        _last_call_ts[0] = time.time()

    if r.status_code != 200:
        print(f'[product-search] HTTP {r.status_code}: {r.text[:200]}', flush=True)
        return []

    items = r.json().get('Items') or []
    results = []
    for wrapper in items[:hits]:
        item = wrapper.get('Item') or {}
        name = (item.get('itemName') or '').strip()
        if not name:
            continue
        price = item.get('itemPrice') or 0
        img = ''
        for img_obj in (item.get('mediumImageUrls') or []):
            url = img_obj.get('imageUrl') or ''
            if url:
                # ?_ex=128x128 サフィックスを大きいサイズに置換
                img = re.sub(r'\?_ex=\d+x\d+$', '?_ex=300x300', url)
                break
        aff_url = (item.get('affiliateUrl') or item.get('itemUrl') or '').strip()
        results.append({
            'name': name,
            'price': int(price),
            'image_url': img,
            'affiliate_url': aff_url,
            'shop': (item.get('shopName') or '').strip(),
        })
    return results


def build_card_html(keyword, product, amazon_tag=''):
    """検索結果1件 + 元キーワードから商品カード HTML を組み立てる。

    インラインスタイル方式: はてなブログのデザインCSSを汚染せず、
    記事HTMLに直接 style 属性を埋め込む。テーマを変えても見た目は壊れない。

    Args:
        keyword: Gemini が指定した商品名(Amazon検索URL生成にも使う)
        product: search() の戻り値の1要素 dict
        amazon_tag: Amazon アソシエイトタグ(あれば付与)

    Returns:
        HTML 文字列
    """
    name = product.get('name', keyword)
    price = product.get('price', 0)
    img = product.get('image_url', '')
    rakuten_url = product.get('affiliate_url', '')

    # Amazon検索URL (tag 付与は既存の _apply_amazon_affiliate に任せる)
    amazon_url = f'https://www.amazon.co.jp/s?k={quote(keyword)}'
    if amazon_tag:
        amazon_url += f'&tag={amazon_tag}'

    # 商品名は楽天の長い名前を80字にカット
    display_name = name if len(name) <= 80 else name[:77] + '…'
    brand = product.get('shop', '')[:30]

    # ===== インラインスタイル定義 =====
    s_card = (
        'display:flex;align-items:center;gap:16px;'
        'margin:24px 0;padding:16px;'
        'border:1px solid #e0e0e0;border-radius:12px;'
        'background:#fff;'
        'box-shadow:0 2px 8px rgba(0,0,0,0.04);'
        'box-sizing:border-box;'
    )
    s_img = (
        'width:160px;height:160px;'
        'object-fit:contain;flex-shrink:0;'
        'background:#f8f8f8;border-radius:8px;'
        'border:none;'
    )
    s_info = 'flex:1;min-width:0;'
    s_name = (
        'font-size:16px;font-weight:700;line-height:1.4;'
        'margin:0 0 6px 0;color:#222;'
    )
    s_brand = 'font-size:12px;color:#888;margin:0 0 6px 0;'
    s_price = 'font-size:18px;font-weight:700;color:#d32f2f;margin:0 0 12px 0;'
    s_btns = 'display:flex;gap:8px;flex-wrap:wrap;'
    s_btn_base = (
        'display:inline-block;padding:10px 16px;'
        'border-radius:6px;font-size:14px;font-weight:700;'
        'text-align:center;text-decoration:none;'
        'color:#fff;flex:1;min-width:110px;'
    )
    s_btn_amazon = s_btn_base + 'background:#FF9900;'
    s_btn_rakuten = s_btn_base + 'background:#BF0000;'

    img_html = (
        f'<img src="{img}" alt="{_html_escape(display_name)}" '
        f'style="{s_img}" loading="lazy" />'
    ) if img else ''
    price_html = (
        f'<p style="{s_price}">楽天市場価格: ¥{price:,}</p>'
    ) if price else ''

    # スマホ対応用に「親カード」「ボタン群」だけ非常にシンプルな構造に
    # → スマホは画面幅 360〜480px 想定で、画像160pxとボタンが横並びでも収まる
    return (
        f'<div style="{s_card}">\n'
        f'  {img_html}\n'
        f'  <div style="{s_info}">\n'
        f'    <h3 style="{s_name}">{_html_escape(display_name)}</h3>\n'
        + (f'    <p style="{s_brand}">{_html_escape(brand)}</p>\n' if brand else '')
        + f'    {price_html}\n'
        f'    <div style="{s_btns}">\n'
        f'      <a style="{s_btn_amazon}" href="{amazon_url}" target="_blank" rel="nofollow sponsored noopener">Amazon</a>\n'
        f'      <a style="{s_btn_rakuten}" href="{_html_escape(rakuten_url)}" target="_blank" rel="nofollow sponsored noopener">楽天市場</a>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'</div>\n'
    )


def _html_escape(s):
    if not s:
        return ''
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


# Gemini が記事内に埋め込むプレースホルダ
_PLACEHOLDER_RE = re.compile(r'\[PRODUCT_CARD:\s*([^\]\n]+?)\s*\]')


# 「誘導文 + カード」がセットとして一体だと判別するためのキーワード
# カードが商品ヒットせず削除された場合、直前の誘導文もセットで消す
_CTA_HINT_WORDS = (
    '気になる人は', '実勢価格', '在庫', '下のカード', '下のリンク',
    'カードから', 'リンクから', 'チェックしておく', '価格をチェック',
    '価格をチェック', '型番を直接確認', '型番を確認', 'こちらのリンク',
    '見ておきたい', '見ておくと', '見て確認', '確認しておく',
    '型番と実勢価格', 'カードで確認',
)


def replace_placeholders(markdown_body, blog):
    """記事本文中の [PRODUCT_CARD: 商品名] を実商品カード HTML に置換。

    商品が楽天で見つからなければプレースホルダ自体を削除する。
    さらに、そのプレースホルダの直前にあった「誘導文」(気になる人はカードを〜等) も
    一緒に削除する。「カードが無いのに『カードを見て』と案内する」という誤誘導を防ぐ。
    """
    if not markdown_body:
        return markdown_body
    if '[PRODUCT_CARD:' not in markdown_body:
        return markdown_body

    amazon_tag = (blog.get('amazon_affiliate_tag') or '').strip()
    seen_keywords = set()

    # 各 [PRODUCT_CARD: xxx] を「カード結果」「未ヒットなら直前段落も削除」で順次処理する。
    # re.sub の repl では「直前を一緒に削除」できないので、手動でループ。
    out = markdown_body
    while True:
        m = _PLACEHOLDER_RE.search(out)
        if not m:
            break
        kw = m.group(1).strip()
        start, end = m.start(), m.end()
        if not kw or kw.lower() in seen_keywords:
            # 重複は単純削除
            out = out[:start] + out[end:]
            continue
        seen_keywords.add(kw.lower())

        products = search(kw, blog, hits=1)
        if not products:
            # 未ヒット: プレースホルダの直前の段落 (誘導文) もまとめて削除
            print(f'[product-card] no hit for "{kw}", removing placeholder + leading CTA', flush=True)
            # プレースホルダ直前のテキストから「直近の段落」を取得 (空行で区切られた塊)
            before = out[:start]
            # 直前段落: 最後の空行 (改行2連続) 以降〜start
            split_at = max(before.rfind('\n\n'), before.rfind('\r\n\r\n'))
            if split_at >= 0:
                lead_para = before[split_at:]
                # その段落に誘導フレーズが含まれていれば、段落ごと削除
                if any(w in lead_para for w in _CTA_HINT_WORDS):
                    out = before[:split_at] + out[end:]
                    continue
            # 誘導文が見つからない場合はプレースホルダだけ削除
            out = before + out[end:]
            continue
        # ヒット: カード HTML に置換
        card = build_card_html(kw, products[0], amazon_tag=amazon_tag)
        print(f'[product-card] OK: {kw} -> {products[0].get("name","")[:40]}', flush=True)
        out = out[:start] + '\n\n' + card + '\n' + out[end:]

    # 連続空行を圧縮
    import re as _re
    out = _re.sub(r'\n{4,}', '\n\n\n', out)
    return out
