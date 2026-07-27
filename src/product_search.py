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

# 関連度判定に使う候補数。1件だけ取ると「レビュー数の多いアクセサリ」を
# 掴まされるので、多めに取ってスコアリングで本命を選ぶ。
_CANDIDATE_POOL = 24

# 商品名に含まれると「本体ではなく周辺アクセサリ」を強く示唆する語。
# キーワード側に同じ語が含まれる場合は減点しない (例: "iPhone ケース" を探しているとき)。
_ACCESSORY_WORDS = (
    'ケース', 'カバー', 'フィルム', '保護', 'ガラス', 'シール', 'ステッカー',
    'ケーブル', '充電器', 'アダプタ', 'アダプター', '変換', 'コネクタ',
    'スタンド', 'ホルダー', 'マウント', '三脚', 'グリップ', 'ストラップ',
    'イヤーピース', 'イヤーパッド', '替え', '交換用', '互換', '純正品ではない',
    'バッグ', 'ポーチ', '収納', 'クリーナー', 'スキンシール',
    '延長', 'ハブ', '分配', 'トラベル', 'キャップ', 'ペン先',
)

# 商品名にあると「中古・ジャンク・セット売り」を示唆する語 (軽い減点)
_NOISE_WORDS = ('中古', 'ジャンク', '訳あり', 'アウトレット', 'まとめ買い', '福袋')

# 型番トークン: 英字と数字が混在する塊 (WH-1000XM5, M90, S6, XM5 など)
_MODEL_TOKEN_RE = re.compile(r'[A-Za-z]+[-]?\d+[A-Za-z0-9\-]*|\d+[A-Za-z]+[A-Za-z0-9\-]*')
# 単語分割 (英数字の連なり / カタカナの連なり)
_WORD_RE = re.compile(r'[A-Za-z0-9]+|[ァ-ヶー]+')


def _normalize(s):
    """全角英数→半角、小文字化、記号を空白に。"""
    if not s:
        return ''
    out = []
    for ch in s:
        code = ord(ch)
        # 全角英数字・全角スペースを半角へ
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        elif code == 0x3000:
            ch = ' '
        out.append(ch)
    s = ''.join(out).lower()
    # 記号類を空白に (型番のハイフンは残す)
    s = re.sub(r'[【】\[\]（）()「」『』/,、。！!？?：:；;＋+*"\'|]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _tokens(s):
    return set(_WORD_RE.findall(_normalize(s)))


def _model_tokens(s):
    """型番らしきトークン (英字+数字混在) を返す。"""
    return set(t.lower() for t in _MODEL_TOKEN_RE.findall(_normalize(s)))


def _relevance_score(keyword, item_name, price, prices_in_pool):
    """キーワードに対する商品名の関連度を 0.0〜1.0 で返す。

    低いほど「関係ない商品」。呼び出し側が閾値でフィルタする。
    """
    kw_norm = _normalize(keyword)
    name_norm = _normalize(item_name)
    if not kw_norm or not name_norm:
        return 0.0

    score = 0.0

    # ---- 1. 型番一致 (最重要) ----
    kw_models = _model_tokens(keyword)
    name_models = _model_tokens(item_name)
    if kw_models:
        matched = kw_models & name_models
        # 型番が1つも一致しない = ほぼ別商品
        if not matched:
            return 0.0
        score += 0.45 * (len(matched) / len(kw_models))
    else:
        # 型番なしキーワード (例: "ワイヤレスイヤホン") は語の一致でみる
        score += 0.20

    # ---- 2. 語の重なり ----
    kw_words = _tokens(keyword)
    name_words = _tokens(item_name)
    if kw_words:
        overlap = len(kw_words & name_words) / len(kw_words)
        score += 0.35 * overlap
        # キーワードの語が半分も入っていない商品名は疑わしい
        if overlap < 0.4:
            score -= 0.15

    # ---- 3. アクセサリ判定 (今回の本命の修正) ----
    # キーワード自体がアクセサリを指している場合は減点しない
    kw_is_accessory = any(w in keyword for w in _ACCESSORY_WORDS)
    if not kw_is_accessory:
        hit = [w for w in _ACCESSORY_WORDS if w in item_name]
        if hit:
            # 「〜用」「〜対応」はアクセサリの決定的シグナル
            if re.search(r'(用|対応|専用)\s*(の)?', item_name):
                return 0.0
            score -= 0.30 * min(len(hit), 2)

    # ---- 4. ノイズ語 ----
    if any(w in item_name for w in _NOISE_WORDS):
        score -= 0.15

    # ---- 5. 価格の妥当性 ----
    # 候補群の中央値より極端に安い = アクセサリの可能性が高い
    if prices_in_pool and price:
        srt = sorted(p for p in prices_in_pool if p > 0)
        if srt:
            median = srt[len(srt) // 2]
            if median > 0 and price < median * 0.15:
                score -= 0.25

    return max(0.0, min(1.0, score))


def _sanitize_keyword(keyword):
    """楽天APIが 400 を返さないようキーワードを整える。"""
    kw = re.sub(r'[【】\[\]｜|/\\"\'<>]', ' ', keyword or '')
    kw = re.sub(r'\s+', ' ', kw).strip()
    # API は長すぎるキーワードを弾く
    return kw[:64]


def _fetch_candidates(keyword, blog, sort):
    """楽天APIを1回叩いて候補リストを返す。失敗時は []。"""
    app_id = (blog.get('rakuten_app_id') or '').strip()
    access_key = (blog.get('rakuten_access_key') or '').strip()
    aff_id = (blog.get('rakuten_affiliate_id') or '').strip()
    kw = _sanitize_keyword(keyword)
    if not (app_id and access_key and kw):
        return []

    domain = (blog.get('hatena_blog_domain') or '').strip()
    origin = f'https://{domain}' if domain else _DEFAULT_ORIGIN

    elapsed = time.time() - _last_call_ts[0]
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    params = {
        'format': 'json',
        'keyword': kw,
        'applicationId': app_id,
        'accessKey': access_key,
        'hits': _CANDIDATE_POOL,
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

    out = []
    for wrapper in (r.json().get('Items') or []):
        item = wrapper.get('Item') or {}
        name = (item.get('itemName') or '').strip()
        if not name:
            continue
        img = ''
        for img_obj in (item.get('mediumImageUrls') or []):
            url = img_obj.get('imageUrl') or ''
            if url:
                img = re.sub(r'\?_ex=\d+x\d+$', '?_ex=300x300', url)
                break
        out.append({
            'name': name,
            'price': int(item.get('itemPrice') or 0),
            'image_url': img,
            'affiliate_url': (item.get('affiliateUrl') or item.get('itemUrl') or '').strip(),
            'shop': (item.get('shopName') or '').strip(),
            'review_count': int(item.get('reviewCount') or 0),
        })
    return out


def search(keyword, blog, hits=1, sort='standard', min_relevance=0.45):
    """楽天市場で keyword を検索し、**キーワードに実際に関連する** 商品を返す。

    候補を _CANDIDATE_POOL 件取得し、型番一致 / 語の重なり / アクセサリ判定 /
    価格の妥当性でスコアリングして並べ替える。閾値を超える商品が1つもなければ
    空リストを返す (= 無関係な商品カードを出すくらいならカードを出さない)。

    Args:
        keyword: 商品名(例: "Sony WH-1000XM5")
        blog: ブログ設定 dict (rakuten_app_id / rakuten_access_key / rakuten_affiliate_id 必要)
        hits: 返す件数
        sort: 楽天APIの並び順。'standard' はキーワード適合度順で、
              '-reviewCount' より本体商品が上位に来やすい
        min_relevance: この関連度未満の商品は捨てる

    Returns:
        [{'name','price','image_url','affiliate_url','shop'}, ...] or []
    """
    # ブランド名を伴わない「型番だけ」のキーワードは検索しない。
    # 例: "DR02" だけだと、たまたま型番が一致する全く無関係な他社製品
    # (別ジャンル・別ブランド) がヒットしても見分けようがない。
    # "iFLYTEK S6" や "Sony WH-1000XM5" のように語が2つ以上あれば通す。
    kw_word_count = len(_tokens(keyword))
    if kw_word_count < 2:
        print(
            f'[product-search] "{keyword}": キーワードにブランド名が無く曖昧なため検索スキップ',
            flush=True,
        )
        return []

    candidates = _fetch_candidates(keyword, blog, sort)
    if not candidates:
        return []

    prices = [c['price'] for c in candidates]
    scored = []
    for c in candidates:
        rel = _relevance_score(keyword, c['name'], c['price'], prices)
        if rel >= min_relevance:
            scored.append((rel, c))

    if not scored:
        best_rel, best_item = max(
            ((_relevance_score(keyword, c['name'], c['price'], prices), c)
             for c in candidates),
            key=lambda pair: pair[0],
        )
        print(
            f'[product-search] "{keyword}": no relevant item '
            f'(best={best_rel:.2f} "{best_item["name"][:40]}")',
            flush=True,
        )
        return []

    # 関連度優先、同点ならレビュー数の多い順
    scored.sort(key=lambda x: (round(x[0], 2), x[1]['review_count']), reverse=True)
    top = scored[:hits]
    print(
        f'[product-search] "{keyword}": {len(scored)}/{len(candidates)} relevant, '
        f'picked "{top[0][1]["name"][:40]}" (rel={top[0][0]:.2f})',
        flush=True,
    )
    return [c for _, c in top]


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


def _mentioned_in_body(keyword, body_without_placeholders):
    """その商品名が本文中で実際に語られているか。

    Gemini が本文で一度も触れていない商品のカードを置くことがあるため、
    「本文に出てこない商品のカードは出さない」ためのチェック。
    型番があれば型番の一致を、無ければ語の過半一致を要求する。
    """
    body = _normalize(body_without_placeholders)
    if not body:
        return False

    models = _model_tokens(keyword)
    if models:
        return any(m in body for m in models)

    words = [w for w in _tokens(keyword) if len(w) >= 2]
    if not words:
        return False
    hit = sum(1 for w in words if w in body)
    return hit >= max(1, len(words) // 2)


def replace_placeholders(markdown_body, blog):
    """記事本文中の [PRODUCT_CARD: 商品名] を実商品カード HTML に置換。

    次のいずれかに当てはまるプレースホルダは削除する:
      - 本文中でその商品に一度も言及していない (記事と無関係なカード)
      - 楽天で「キーワードに実際に関連する」商品が見つからない

    さらに、そのプレースホルダの直前にあった「誘導文」(気になる人はカードを〜等) も
    一緒に削除する。「カードが無いのに『カードを見て』と案内する」という誤誘導を防ぐ。
    """
    if not markdown_body:
        return markdown_body
    if '[PRODUCT_CARD:' not in markdown_body:
        return markdown_body

    amazon_tag = (blog.get('amazon_affiliate_tag') or '').strip()
    seen_keywords = set()

    # 言及チェック用に、プレースホルダを除いた本文を用意しておく
    # (プレースホルダ自身を「言及」とカウントしないため)
    body_text_only = _PLACEHOLDER_RE.sub(' ', markdown_body)

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

        if not _mentioned_in_body(kw, body_text_only):
            print(f'[product-card] "{kw}" not mentioned in body, dropping card', flush=True)
            products = []
        else:
            try:
                products = search(kw, blog, hits=1)
            except Exception as e:
                # 1商品の検索失敗で記事全体のカード変換を巻き込まない。
                # このプレースホルダだけ「未ヒット」扱いにして処理を継続する。
                print(f'[product-card] search error for "{kw}": {e}', flush=True)
                products = []
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
