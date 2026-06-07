"""Firestore-backed multi-blog management."""
import hashlib
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from config import HATENA_ID, HATENA_API_KEY, HATENA_BLOG_DOMAIN
from src.vocab import generate_vocab, merge_with_default

_VOCAB_RELEVANT_FIELDS = ('genre', 'article_policy', 'topic_policy', 'tone_prompt')


_DEFAULT_GADGET_FEEDS = [
    # ★★ AR/XR/VR 専門メディア (ブログの主軸) ★★
    'https://www.roadtovr.com/feed/',                     # AR/VR の老舗専門メディア
    'https://www.uploadvr.com/rss/',                       # VR 専門
    'https://mixed-news.com/en/feed/',                     # XR ニュース 英語版
    'https://xrtoday.com/feed/',                           # XR Today
    'https://arpost.co/feed/',                             # AR Post
    'https://vrscout.com/feed/',                           # VR Scout
    # ハードウェア大手 (AR/VR の発表・周辺ガジェット)
    'https://www.theverge.com/rss/index.xml',              # AR/VR セクションも強い
    'https://9to5google.com/feed/',                         # Google Project Aura / Android XR
    'https://9to5mac.com/feed/',                            # Apple Vision Pro
    'https://feeds.macrumors.com/MacRumors-All',            # Vision Pro / Apple AR
    # ニッチ・面白ガジェット系
    'https://hackaday.com/blog/feed/',                      # DIY/ハードウェア (Kickstarter 系)
    'https://www.engadget.com/rss.xml',                     # ガジェット全般 (フィルタリング前提)
    # 国内 (AR/VR 関連記事拾い用)
    'https://www.gizmodo.jp/index.xml',                     # AR/VR トレンドキャッチ
    'https://www.moguravr.com/feed/',                       # 国内 VR/AR メディア
]

_client = None


def _coll():
    global _client
    if _client is None:
        _client = firestore.Client()
    return _client.collection('blogs')


def _enrich(blog):
    """fetched blog dict に vocab を必ず存在させる (欠けたキーはデフォルトで埋める)。"""
    blog['vocab'] = merge_with_default(blog.get('vocab'))
    return blog


def list_blogs():
    docs = _coll().order_by('created_at').stream()
    return [_enrich({'id': d.id, **d.to_dict()}) for d in docs]


def get_blog(blog_id):
    doc = _coll().document(blog_id).get()
    if not doc.exists:
        return None
    return _enrich({'id': doc.id, **doc.to_dict()})


def create_blog(data):
    now = datetime.now(timezone.utc)
    payload = _normalize(data)
    payload['created_at'] = now
    payload['updated_at'] = now
    try:
        payload['vocab'] = generate_vocab(payload)
    except Exception as e:
        print(f'[blogs] vocab gen failed on create: {e}', flush=True)
    ref = _coll().document()
    ref.set(payload)
    return ref.id


def update_blog(blog_id, data):
    payload = _normalize(data)
    payload['updated_at'] = datetime.now(timezone.utc)

    # ジャンル等が変わったらvocabを再生成
    existing = _coll().document(blog_id).get()
    prev = existing.to_dict() if existing.exists else {}
    relevant_changed = any(
        (prev.get(k) or '') != payload.get(k, '')
        for k in _VOCAB_RELEVANT_FIELDS
    )
    if relevant_changed or not prev.get('vocab'):
        try:
            payload['vocab'] = generate_vocab(payload)
        except Exception as e:
            print(f'[blogs] vocab gen failed on update: {e}', flush=True)
    _coll().document(blog_id).set(payload, merge=True)


def regenerate_vocab(blog_id):
    """既存ブログの vocab を強制再生成する。"""
    doc = _coll().document(blog_id).get()
    if not doc.exists:
        return None
    blog = doc.to_dict()
    vocab = generate_vocab(blog)
    _coll().document(blog_id).set({'vocab': vocab, 'updated_at': datetime.now(timezone.utc)}, merge=True)
    return vocab


def delete_blog(blog_id):
    _coll().document(blog_id).delete()


def _normalize(data):
    """Strip whitespace and ensure list field is a list."""
    out = {}
    for key in (
        'name', 'hatena_id', 'hatena_api_key', 'hatena_blog_domain',
        'genre', 'niche_focus', 'hatena_category', 'tone_prompt', 'article_policy',
        'topic_policy', 'amazon_affiliate_tag', 'rakuten_affiliate_id',
        'rakuten_app_id', 'rakuten_access_key',
        'indexnow_key', 'hatena_group_banners',
        'x_api_key', 'x_api_secret', 'x_access_token', 'x_access_token_secret',
        'x_template',
        'threads_access_token', 'threads_user_id', 'threads_template',
    ):
        out[key] = (data.get(key) or '').strip()

    out['use_charts'] = bool(data.get('use_charts'))
    out['use_wiki_images'] = bool(data.get('use_wiki_images'))
    out['use_claude_writing'] = bool(data.get('use_claude_writing'))
    out['auto_publish_enabled'] = bool(data.get('auto_publish_enabled'))
    out['x_auto_post_enabled'] = bool(data.get('x_auto_post_enabled'))
    out['threads_auto_post_enabled'] = bool(data.get('threads_auto_post_enabled'))
    out['auto_star_enabled'] = bool(data.get('auto_star_enabled'))

    out['auto_star_max_per_day'] = max(1, min(_to_int('auto_star_max_per_day', 5), 50))
    out['auto_star_hours_jst'] = (data.get('auto_star_hours_jst') or '9-22').strip() or '9-22'
    try:
        p_raw = data.get('auto_star_probability')
        p_val = float(p_raw) if p_raw not in (None, '') else 0.25
    except (TypeError, ValueError):
        p_val = 0.25
    out['auto_star_probability'] = max(0.05, min(p_val, 0.8))

    def _to_int(key, default):
        raw = data.get(key)
        if raw in (None, ''):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    out['auto_min_articles'] = max(0, min(_to_int('auto_min_articles', 0), 50))
    out['auto_max_articles'] = max(max(out['auto_min_articles'], 1), min(_to_int('auto_max_articles', 10), 50))

    feeds = data.get('custom_rss_feeds')
    if isinstance(feeds, str):
        feeds = [u.strip() for u in feeds.splitlines() if u.strip()]
    elif feeds is None:
        feeds = []
    out['custom_rss_feeds'] = feeds
    return out


def feeds_for_blog(blog):
    """Build (name, url) feed list for a blog: Google News by genre + custom RSS."""
    feeds = []
    genre = (blog.get('genre') or '').strip()
    if genre:
        feeds.append((
            f'Google News ({genre})',
            f'https://news.google.com/rss/search?q={quote(genre)}&hl=ja&gl=JP&ceid=JP:ja',
        ))
    for url in (blog.get('custom_rss_feeds') or []):
        url = (url or '').strip()
        if url:
            feeds.append((url, url))
    return feeds


def _posted_doc_id(blog_id, url):
    return hashlib.sha256(f'{blog_id}:{url}'.encode('utf-8')).hexdigest()[:32]


def record_posted_urls(blog_id, urls):
    """Record source URLs that were used in a published article."""
    if not urls:
        return
    coll = _client.collection('posted_links') if _client else None
    if coll is None:
        # ensure client initialized
        _coll()
        coll = _client.collection('posted_links')
    now = datetime.now(timezone.utc)
    for url in urls:
        if not url:
            continue
        doc_id = _posted_doc_id(blog_id, url)
        coll.document(doc_id).set({
            'blog_id': blog_id,
            'url': url,
            'posted_at': now,
        })


_KEYWORD_STOPWORDS = {
    # URL / Markdown 残骸
    'http', 'https', 'www', 'com', 'jpg', 'png', 'gif', 'webp', 'svg',
    'rss', 'feed', 'rss20', 'utf', 'xml', 'src', 'href', 'alt',
    # Hatena/Fotolife ドメイン部分
    'st-hatena', 'cdn-ak', 'cdn', 'fotolife', 'hatenablog', 'hatena',
    'tako-chan', 'tako-karamaru', 'images', 'image', 'photo', 'photos',
    # 一般 HTML 残骸
    'div', 'span', 'class', 'style', 'aria',
    # 日本語ストップワード
    'こと', 'もの', 'ため', 'よう', 'これ', 'それ', 'あれ', 'する', 'なる',
    'ある', 'いる', 'できる', 'について', 'として', 'のように', 'ですが',
    'はてな', 'ブログ', '記事', '紹介', 'まとめ', 'はこちら',
}


def _extract_keywords(title, body):
    """記事タイトル・本文から内部リンク用キーワードを抽出。

    抽出対象:
    - 英数字の連続 (iPhone, Pixel10, MacBook等の製品名)
    - カタカナ連続 3文字以上 (カタカナの製品名・固有名詞)
    - 漢字 2文字以上 (固有名詞っぽいもの)
    - 数字付き型番

    重要: URL / Markdown 構文 / HTML タグは事前に除去してから抽出する
    (cdn-ak, st-hatena, fotolife 等の URL 部品が混入するのを防ぐ)
    """
    # 1. 画像 ![alt](url) 全削除 (alt 内のテキストも捨てる、URL汚染避けるため)
    clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', (body or '')[:3000])
    # 2. リンク [text](url) は text だけ残す
    clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)
    # 3. 生 URL を完全除去
    clean = re.sub(r'https?://\S+', '', clean)
    # 4. HTML タグ除去
    clean = re.sub(r'<[^>]+>', '', clean)
    # 5. ハッシュタグ・はてな記法 [blog:g:...:banner] 除去
    clean = re.sub(r'\[[a-zA-Z]+:[^\]]+\]', '', clean)

    text = f'{title or ""} {clean[:1500]}'

    tokens = []
    # 英数字 (3文字以上)
    tokens.extend(re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}', text))
    # カタカナ (3文字以上)
    tokens.extend(re.findall(r'[゠-ヿー]{3,}', text))
    # 漢字 2文字以上
    tokens.extend(re.findall(r'[一-龯]{2,}', text))
    # 数字付き型番
    tokens.extend(re.findall(r'\d{3,5}', text))

    out = []
    seen = set()
    for t in tokens:
        norm = t.lower().strip()
        if not norm or norm in _KEYWORD_STOPWORDS or len(norm) < 3:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out[:30]


def delete_published_article_record(article_url):
    """指定 URL の published_articles レコードを削除 (記事自体は Hatena 側で別途削除)。"""
    if not article_url:
        return False
    _coll()
    doc_id = hashlib.sha256(article_url.encode('utf-8')).hexdigest()[:32]
    _client.collection('published_articles').document(doc_id).delete()
    return True


def record_published_article(blog_id, title, body, article_url):
    """投稿済み記事を Firestore に記録 (内部リンクで参照するため)。"""
    if not article_url or not title:
        return
    _coll()  # ensure client
    keywords = _extract_keywords(title, body)
    doc_id = hashlib.sha256(article_url.encode('utf-8')).hexdigest()[:32]
    _client.collection('published_articles').document(doc_id).set({
        'blog_id': blog_id,
        'title': title,
        'url': article_url,
        'keywords': keywords,
        'published_at': datetime.now(timezone.utc),
    })


def find_related_articles(blog_id, current_title, current_topic, limit=5, exclude_url=None):
    """同じブログの過去記事から、キーワード重複度が高いものを返す。

    戻り値: [{'title', 'url', 'score'}, ...]
    """
    _coll()
    current_keywords = set(_extract_keywords(current_title, current_topic))
    if not current_keywords:
        return []

    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())

    scored = []
    for d in docs:
        data = d.to_dict()
        if exclude_url and data.get('url') == exclude_url:
            continue
        kws = set(data.get('keywords') or [])
        score = len(current_keywords & kws)
        if score > 0:
            scored.append({
                'title': data.get('title', ''),
                'url': data.get('url', ''),
                'score': score,
            })
    scored.sort(key=lambda x: -x['score'])
    return scored[:limit]


def get_recent_article_keywords(blog_id, days=7, limit_articles=100):
    """過去 N 日間の投稿記事のキーワードを集約して返す。後方互換用 (現在は重み付き版を使う)。"""
    _coll()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    from collections import Counter
    counter = Counter()
    for d in docs:
        data = d.to_dict()
        ts = data.get('published_at')
        if ts and ts < cutoff:
            continue
        for kw in (data.get('keywords') or [])[:10]:
            if kw and len(kw) >= 2:
                counter[kw.lower()] += 1
    return [k for k, _ in counter.most_common(50)]


def get_recent_published_titles(blog_id, days=30):
    """過去 N 日間に公開した記事タイトル一覧を返す。
    Geminiのトピック選定プロンプトで「これらと類似のテーマを選ばない」リストとして使う。
    """
    _coll()
    now = datetime.now(timezone.utc)
    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    titles = []
    for d in docs:
        data = d.to_dict()
        ts = data.get('published_at')
        if not ts:
            continue
        try:
            days_ago = (now - ts).total_seconds() / 86400.0
        except Exception:
            continue
        if 0 <= days_ago <= days:
            t = (data.get('title') or '').strip()
            if t:
                titles.append({'title': t, 'days_ago': days_ago})
    titles.sort(key=lambda x: x['days_ago'])
    return [t['title'] for t in titles]


def get_recent_keyword_weights(blog_id, decay_days=7, lookback_days=14):
    """過去 N 日間の投稿記事のキーワードに時間減衰重みを付けて返す。

    新しい記事ほど重みが大きく、古いものはほぼ0に。
    重み式: max(0, 1 - days_ago / decay_days) — decay_days 経過で 0 に。
    auto_runner の多様性ペナルティで「同じテーマを直近で書きすぎない」判定に使う。

    Returns: dict {keyword_lower: float_weight}
    """
    _coll()
    now = datetime.now(timezone.utc)
    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    weights = {}
    for d in docs:
        data = d.to_dict()
        ts = data.get('published_at')
        if not ts:
            continue
        try:
            days_ago = (now - ts).total_seconds() / 86400.0
        except Exception:
            continue
        if days_ago < 0 or days_ago > lookback_days:
            continue
        w = max(0.0, 1.0 - days_ago / float(decay_days))
        if w <= 0:
            continue
        for kw in (data.get('keywords') or [])[:10]:
            if kw and len(kw) >= 2:
                key = kw.lower()
                weights[key] = weights.get(key, 0.0) + w
    return weights


def list_published_articles(blog_id, limit=500):
    """投稿済み記事を新着順に取得。"""
    _coll()
    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    out = []
    for d in docs:
        data = d.to_dict()
        out.append({
            'title': data.get('title', ''),
            'url': data.get('url', ''),
            'keywords': data.get('keywords') or [],
            'published_at': data.get('published_at'),
        })
    out.sort(key=lambda a: a.get('published_at') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]


def cluster_articles(blog_id, min_cluster_size=2):
    """投稿済み記事をキーワード別にクラスタリング。

    戻り値: {
        'total': N,
        'clusters': [{'keyword', 'count', 'articles': [...]}, ...],
    }
    """
    articles = list_published_articles(blog_id)
    keyword_to_articles = {}
    for a in articles:
        # 各記事につき上位5キーワードに限定 (最も特徴的なキーワード)
        for kw in a.get('keywords', [])[:5]:
            keyword_to_articles.setdefault(kw, []).append({
                'title': a['title'], 'url': a['url'],
            })

    clusters = [
        {'keyword': k, 'count': len(v), 'articles': v}
        for k, v in keyword_to_articles.items()
        if len(v) >= min_cluster_size
    ]
    clusters.sort(key=lambda c: -c['count'])
    return {'total': len(articles), 'clusters': clusters}


def get_posted_urls(blog_id):
    """Return set of URLs already used in published articles for this blog."""
    _coll()  # ensure client
    docs = (_client.collection('posted_links')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    return {d.to_dict().get('url') for d in docs if d.to_dict().get('url')}


# ---------- pending review (自動運用でコンプラ違反になった下書き) ----------

def save_pending_review(blog_id, title, body, source_urls, issues_text, quality_score=None):
    """自動運用でBLOCKがついた記事を下書き退避する。

    後でユーザーがWeb UIから承認/破棄するためのキュー。
    """
    _coll()
    now = datetime.now(timezone.utc)
    doc_id = hashlib.sha256(f'{blog_id}:{now.isoformat()}:{title}'.encode('utf-8')).hexdigest()[:32]
    _client.collection('pending_reviews').document(doc_id).set({
        'blog_id': blog_id,
        'title': title,
        'body': body,
        'source_urls': source_urls or [],
        'issues_text': issues_text or '',
        'quality_score': quality_score,
        'created_at': now,
    })
    return doc_id


def list_pending_reviews(blog_id=None, limit=50):
    _coll()
    coll = _client.collection('pending_reviews')
    if blog_id:
        docs = coll.where(filter=FieldFilter('blog_id', '==', blog_id)).stream()
    else:
        docs = coll.stream()
    out = []
    for d in docs:
        data = d.to_dict()
        out.append({'id': d.id, **data})
    out.sort(key=lambda a: a.get('created_at') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]


def count_pending_reviews(blog_id):
    """ホーム画面のバッジ用。"""
    try:
        _coll()
        docs = list(_client.collection('pending_reviews')
                    .where(filter=FieldFilter('blog_id', '==', blog_id))
                    .stream())
        return len(docs)
    except Exception:
        return 0


def get_pending_review(review_id):
    _coll()
    doc = _client.collection('pending_reviews').document(review_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


def delete_pending_review(review_id):
    _coll()
    _client.collection('pending_reviews').document(review_id).delete()


# ---------- starred_urls (auto_star 機能: 既スターURLの dedup 用) ----------

def record_starred_url(blog_id, target_url, title=''):
    _coll()
    now = datetime.now(timezone.utc)
    doc_id = hashlib.sha256(f'{blog_id}:{target_url}'.encode('utf-8')).hexdigest()[:32]
    _client.collection('starred_urls').document(doc_id).set({
        'blog_id': blog_id,
        'url': target_url,
        'title': title,
        'starred_at': now,
    })


def get_starred_urls(blog_id):
    _coll()
    docs = (_client.collection('starred_urls')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    return {d.to_dict().get('url') for d in docs if d.to_dict().get('url')}


def update_threads_token(blog_id, new_token, new_expires_at):
    """threads_token_refresh から呼ばれるトークン更新用ヘルパー。"""
    _coll().document(blog_id).set({
        'threads_access_token': new_token,
        'threads_token_expires_at': new_expires_at,
        'updated_at': datetime.now(timezone.utc),
    }, merge=True)


def get_recent_published_articles(blog_id, hours=24, limit=10):
    """過去N時間に公開した記事を新しい順で返す。Threads daily digest 用。"""
    _coll()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    docs = (_client.collection('published_articles')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    arts = []
    for d in docs:
        data = d.to_dict()
        ts = data.get('published_at')
        if not ts or ts < cutoff:
            continue
        arts.append({
            'title': data.get('title', ''),
            'url': data.get('url', ''),
            'keywords': data.get('keywords') or [],
            'published_at': ts,
        })
    arts.sort(key=lambda a: a['published_at'], reverse=True)
    return arts[:limit]


def count_stars_today(blog_id):
    """JST の今日 (00:00以降) に付けたスター数を返す。

    Firestore 複合インデックスを避けるため blog_id だけで絞り込み、
    日付フィルタは Python 側で実施 (1ブログあたり総スター数は小さいので問題なし)。
    """
    _coll()
    jst = timezone(timedelta(hours=9))
    today_start_jst = datetime.now(jst).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_jst.astimezone(timezone.utc)
    docs = (_client.collection('starred_urls')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    count = 0
    for d in docs:
        ts = d.to_dict().get('starred_at')
        if ts and ts >= today_start_utc:
            count += 1
    return count


# ---------- returned_stars (return-star 機能: 返したユーザーを dedup) ----------

def record_returned_star(blog_id, hatena_username, target_url):
    _coll()
    doc_id = hashlib.sha256(f'{blog_id}:{hatena_username}'.encode('utf-8')).hexdigest()[:32]
    _client.collection('returned_stars').document(doc_id).set({
        'blog_id': blog_id,
        'starer_username': hatena_username,
        'target_url': target_url,
        'returned_at': datetime.now(timezone.utc),
    })


def get_returned_star_users(blog_id):
    _coll()
    docs = (_client.collection('returned_stars')
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .stream())
    return {d.to_dict().get('starer_username') for d in docs if d.to_dict().get('starer_username')}


def seed_default_blog_if_empty():
    """Seed one blog from .env values if none exist."""
    if list_blogs():
        return None
    if not (HATENA_ID and HATENA_API_KEY and HATENA_BLOG_DOMAIN):
        return None
    return create_blog({
        'name': f'{HATENA_BLOG_DOMAIN} (初期登録)',
        'hatena_id': HATENA_ID,
        'hatena_api_key': HATENA_API_KEY,
        'hatena_blog_domain': HATENA_BLOG_DOMAIN,
        'genre': 'ガジェット ニュース テクノロジー',
        'hatena_category': 'ガジェットニュース',
        'tone_prompt': '速報感のある報道調。客観的・事実中心で淡々と紹介する。ですます調。「〜と発表されました」「〜と公開された」など報道スタイル。個人感想・主観表現は最小限。',
        'article_policy': (
            '【ガジェット・テック・ニュース速報スタイル】\n'
            '海外含む最新ニュースを淡々とわかりやすくまとめる。\n'
            '構成: ① 結論サマリ (何が起きたか1段落) → ② 経緯・背景 → ③ 主な発表内容/スペック → '
            '④ 意義・読者への影響 → ⑤ 今後の見通し → まとめ → よくある質問\n'
            '海外メディアが情報源の場合は出典名を必ず明記: 「The Verge によると」「9to5Mac の報道では」「MacRumors は〜と伝えている」など。\n'
            '英語ソースの内容は日本語で正確に翻訳・要約する。直訳ではなく自然な日本語に。\n'
            '個人感想は最小限。読者にとっての「で、これは何が新しい？誰に嬉しい？」を必ず明示する。'
        ),
        'amazon_affiliate_tag': '',
        'custom_rss_feeds': list(_DEFAULT_GADGET_FEEDS),
    })
