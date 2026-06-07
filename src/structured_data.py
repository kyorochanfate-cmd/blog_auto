"""記事末尾に schema.org JSON-LD を生成する。

Hatenaブログのエントリー本文に <script type="application/ld+json"> を入れると
Googleがそれを構造化データとして解釈する (検索結果のリッチスニペット対象)。
"""
import json
import re
from datetime import datetime, timezone


_HEADING_RE = re.compile(r'^##\s+(.+?)\s*$', re.MULTILINE)
_FAQ_HEADING_RE = re.compile(r'^##\s*(?:よくある質問|Q&A|FAQ).*$', re.MULTILINE | re.IGNORECASE)

# HowTo schema 用: 「## ステップ1:」「## 手順1:」「## ① ...」「## Step 1:」のような手順型H2
_HOWTO_STEP_RE = re.compile(
    r'^##\s+(?:ステップ\s*[\d①-⑩一二三四五六七八九十]+|'
    r'手順\s*\d+|Step\s*\d+|'
    r'[①-⑩][\s::]?|'
    r'\d+\.\s)\s*[::]?\s*(.+?)$',
    re.MULTILINE | re.IGNORECASE,
)
_HOWTO_TITLE_KWS = ('方法', 'やり方', '手順', '設定方法', '使い方', 'how to', 'やってみた', 'ガイド')

# Review schema 用: タイトル/H2 にレビュー語、★形式の評価
_REVIEW_TITLE_KWS = ('レビュー', '評価', '使ってみた', '感想', '実機', 'review')
_RATING_RE = re.compile(r'(?:★+|☆+|\bstar)|(?:[1-5]\.?[05]?)\s*/\s*5')


def _extract_first_image(markdown_body):
    m = re.search(r'!\[[^\]]*\]\(([^)\s]+)', markdown_body)
    return m.group(1) if m else None


def _extract_description(markdown_body, max_chars=160):
    """最初の段落を抽出してメタディスクリプションに使う。"""
    for line in markdown_body.splitlines():
        s = line.strip()
        if not s or s.startswith('#') or s.startswith('>') or s.startswith('!') or s.startswith('|') or s.startswith('-'):
            continue
        # Markdown記号を雑に除去
        s = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', s)
        s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)
        if len(s) > max_chars:
            s = s[:max_chars - 1] + '…'
        return s
    return ''


def _extract_faqs(markdown_body):
    """## よくある質問 セクション以下から Q/A ペアを抽出する。

    対応フォーマット例:
      ### Q. xxx
      A. yyy
    または:
      **Q. xxx**
      yyy
    または:
      ### xxx？
      yyy
    """
    m = _FAQ_HEADING_RE.search(markdown_body)
    if not m:
        return []
    section = markdown_body[m.end():]
    # 次の H2 までを切り出す
    next_h2 = re.search(r'^##\s+', section, re.MULTILINE)
    if next_h2:
        section = section[:next_h2.start()]

    qa = []
    # ### / **Q** / Q. を質問とみなす
    chunks = re.split(r'\n(?=###\s|\*\*Q[\.:：]|Q[\.:：])', section)
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        # 質問行抽出
        first_line, _, rest = ch.partition('\n')
        q = first_line.lstrip('#').strip().lstrip('*').rstrip('*').strip()
        q = re.sub(r'^Q[\.:：]\s*', '', q)
        # 回答 (A. を除去)
        a = rest.strip()
        a = re.sub(r'^A[\.:：]\s*', '', a, flags=re.IGNORECASE)
        # Markdown記号を簡易クリーン
        a = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', a)
        a = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', a)
        a = a.strip()
        if q and a and len(q) < 200:
            qa.append({'q': q, 'a': a[:600]})
    return qa[:10]


def _extract_h2_product_names(markdown_body):
    """ランキング/比較記事の H2 から製品名候補を抽出する。

    「## 第N位: 製品名」「## 製品名 の特徴」などのパターン
    """
    names = []
    for m in _HEADING_RE.finditer(markdown_body):
        h = m.group(1).strip()
        # よくある質問・まとめ・比較表など本文セクションを除外
        if any(kw in h for kw in ('よくある質問', 'FAQ', 'まとめ', '比較表', '一目でわかる', '選定基準', '選び方', 'おすすめな人', 'どっちを選ぶ')):
            continue
        # 「第N位: XXX」→ XXX
        m_rank = re.match(r'^第\s*\d+\s*位[:：]?\s*(.+)$', h)
        if m_rank:
            names.append(m_rank.group(1).strip())
            continue
        # 「XXX の特徴」「XXX について」→ XXX
        for suffix in (' の特徴', 'の特徴', 'について', 'のレビュー', 'を徹底解説'):
            if h.endswith(suffix):
                names.append(h[:-len(suffix)].strip())
                break
        else:
            # 製品名っぽい (英数字を含む短い見出し) のみ採用
            if 2 <= len(h) <= 60 and re.search(r'[A-Za-z0-9]', h):
                names.append(h)
    # 重複除去 (順序保持)
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _detect_howto(title, body):
    """この記事が HowTo (手順記事) かを判定。"""
    title_l = (title or '').lower()
    if any(kw in title_l for kw in _HOWTO_TITLE_KWS if kw != 'ガイド'):
        # 「設定方法」「○○のやり方」「使い方ガイド」等
        return True
    # ステップ型 H2 が 3つ以上あれば HowTo
    steps = _HOWTO_STEP_RE.findall(body or '')
    return len(steps) >= 3


def _extract_howto_steps(body):
    """ステップ型 H2 から手順を抽出する。

    Returns: [{'name': 'ステップ名', 'text': '本文要約'}, ...]
    """
    matches = list(_HOWTO_STEP_RE.finditer(body or ''))
    if not matches:
        return []
    steps = []
    for i, m in enumerate(matches):
        step_name = m.group(1).strip()
        # 次のステップ or 次のH2 までを本文として抽出
        start = m.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            # 次のH2を探す
            next_h2 = re.search(r'^##\s', body[start:], re.MULTILINE)
            end = start + next_h2.start() if next_h2 else len(body)
        chunk = body[start:end].strip()
        # 最初の段落 (200字まで) を text に
        first_para = ''
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('|') or line.startswith('>') or line.startswith('-'):
                if first_para:
                    break
                continue
            first_para = (first_para + ' ' + line).strip() if first_para else line
            if len(first_para) >= 200:
                break
        first_para = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', first_para)
        first_para = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', first_para)
        steps.append({'name': step_name[:80], 'text': first_para[:300]})
    return steps


def _detect_review_with_rating(title, body):
    """この記事がレビュー記事で、かつ評価値があるかを判定。

    Google Review schema は rating 必須なので、両方揃ったときだけ True。
    """
    title_l = (title or '').lower()
    body_text = body or ''
    is_review = any(kw in title_l for kw in _REVIEW_TITLE_KWS)
    if not is_review:
        # 本文に「期待度: ★★★★☆」のような明示的評価があれば Review 扱い
        if re.search(r'期待度[::]?\s*[★☆]+|評価[::]?\s*[★☆]+|スコア[::]?\s*[\d.]+\s*/\s*5', body_text):
            is_review = True
    if not is_review:
        return False, None
    # 評価値抽出
    rating = _extract_rating(body_text)
    if rating is None:
        return False, None
    return True, rating


def _extract_rating(body):
    """記事本文から数値評価を抽出する。

    パターン:
      - 「★★★★☆」(5段階表示)
      - 「期待度: ★★★★ (4/5)」
      - 「4.5/5」
      - 「期待度スコア: 4.5」
    Returns: float (1-5) or None
    """
    if not body:
        return None
    # 「X/5」「X.X/5」
    m = re.search(r'([1-5](?:\.\d)?)\s*/\s*5', body)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # ★の数 (最初の出現を採用)
    m = re.search(r'(★+)', body)
    if m:
        return float(min(len(m.group(1)), 5))
    return None


def build_jsonld(title, markdown_body, kind='article', author_name='', site_url='', products=None):
    """記事のメタデータからJSON-LDを生成する。

    kind: 'article' / 'comparison' / 'ranking'
    products: 比較・ランキングの製品名リスト (kind != 'article' 時)
    戻り値: '<script type="application/ld+json">...</script>' 形式の文字列
    """
    blocks = []

    image = _extract_first_image(markdown_body)
    description = _extract_description(markdown_body)
    now_iso = datetime.now(timezone.utc).isoformat()

    article_obj = {
        '@context': 'https://schema.org',
        '@type': 'Article',
        'headline': title,
        'description': description,
        'datePublished': now_iso,
        'dateModified': now_iso,
    }
    if image:
        article_obj['image'] = image
    if author_name:
        article_obj['author'] = {'@type': 'Person', 'name': author_name}
    if site_url:
        article_obj['publisher'] = {'@type': 'Organization', 'name': author_name or site_url, 'url': site_url}
    blocks.append(article_obj)

    # FAQ
    faqs = _extract_faqs(markdown_body)
    if faqs:
        blocks.append({
            '@context': 'https://schema.org',
            '@type': 'FAQPage',
            'mainEntity': [
                {
                    '@type': 'Question',
                    'name': qa['q'],
                    'acceptedAnswer': {'@type': 'Answer', 'text': qa['a']},
                }
                for qa in faqs
            ],
        })

    # HowTo schema (手順型記事 = 「方法」「やり方」「設定」 or ステップ型H2 3つ以上)
    if _detect_howto(title, markdown_body):
        steps = _extract_howto_steps(markdown_body)
        if len(steps) >= 2:
            blocks.append({
                '@context': 'https://schema.org',
                '@type': 'HowTo',
                'name': title,
                'description': description[:160] if description else title,
                'step': [
                    {
                        '@type': 'HowToStep',
                        'position': i + 1,
                        'name': s['name'],
                        'text': s['text'] or s['name'],
                    }
                    for i, s in enumerate(steps)
                ],
            })

    # Review schema (レビュー記事 + 明示的な評価値あり)
    is_review, rating = _detect_review_with_rating(title, markdown_body)
    if is_review and rating is not None:
        blocks.append({
            '@context': 'https://schema.org',
            '@type': 'Review',
            'name': title,
            'reviewBody': description,
            'reviewRating': {
                '@type': 'Rating',
                'ratingValue': rating,
                'bestRating': 5,
                'worstRating': 1,
            },
            'author': {'@type': 'Person', 'name': author_name or 'たこちゃん'},
        })

    # ランキング・比較
    if kind in ('ranking', 'comparison'):
        names = list(products or []) or _extract_h2_product_names(markdown_body)
        if names:
            blocks.append({
                '@context': 'https://schema.org',
                '@type': 'ItemList',
                'itemListOrder': 'https://schema.org/ItemListOrderAscending' if kind == 'ranking' else 'https://schema.org/ItemListUnordered',
                'numberOfItems': len(names),
                'itemListElement': [
                    {
                        '@type': 'ListItem',
                        'position': i + 1,
                        'item': {'@type': 'Product', 'name': n},
                    }
                    for i, n in enumerate(names)
                ],
            })

    if not blocks:
        return ''

    scripts = '\n'.join(
        f'<script type="application/ld+json">\n{json.dumps(b, ensure_ascii=False, indent=2)}\n</script>'
        for b in blocks
    )
    return scripts


def append_jsonld(markdown_body, jsonld_html):
    """記事末尾にJSON-LDを差し込む。既に入っていれば置換する。"""
    if not jsonld_html:
        return markdown_body
    pattern = r'\n*<!--\s*structured-data-start\s*-->[\s\S]*?<!--\s*structured-data-end\s*-->\s*$'
    block = (
        '\n\n<!-- structured-data-start -->\n'
        + jsonld_html
        + '\n<!-- structured-data-end -->\n'
    )
    if re.search(pattern, markdown_body):
        return re.sub(pattern, block, markdown_body)
    return markdown_body.rstrip() + block
