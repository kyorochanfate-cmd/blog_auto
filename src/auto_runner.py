"""1ブログ分の自動運用パイプライン。

人間操作の代替。Cloud Scheduler から呼ばれる前提。

フロー:
  1. ブログ設定取得 → 件数目標を決定 (ニュース価値スコアで 3〜10 件)
  2. トピック選定 (policy-mode は generate_policy_topics、それ以外は select_top_topics)
  3. 各トピックについて:
     a. 参考記事取得 (researcher)
     b. 記事生成 (article_generator)
     c. 画像 rehost (image_rehost) — 外部URLは Fotolife へ。幻覚URLはここで除去される
     d. コンプラ自己検査 (compliance) — BLOCK あれば下書きへ
     e. 品質スコアが低ければ revise_article で1回自動改善
     f. 投稿 (hatena_publisher) or 下書きFirestore保存
  4. 通知メール (NG/失敗あった場合のみ)

差別化のための工夫:
  - 同じブログ内で過去使ったソースURLは除外 (posted_links コレクション)
  - 関連過去記事を内部リンクとして必ず1〜2本貼る (find_related_articles)
  - ニュース性スコアの低いトピックは記事化しない (薄い記事の量産を避ける)
"""
import re
import traceback
from datetime import datetime, timezone

from src.news_collector import collect_recent_items
from src.topic_selector import select_top_topics, generate_policy_topics
from src.researcher import fetch_article_texts
from src.article_generator import generate_article, revise_article, pillarize_article
from src.image_finder import find_images, find_images_smart, resolve_official_image
from src.image_rehost import rehost_external_images
from src.hatena_publisher import publish
from src.structured_data import build_jsonld, append_jsonld
from src.quality_scorer import score_article, build_improvement_instructions
from src.indexnow import submit_url as indexnow_submit
from src.compliance import check_article, format_issues_for_human
from src.longtail import suggest_longtail
from src.categorizer import suggest_categories
from src import product_search
from src import google_indexing
from src import notifier
from src import x_poster
from src import threads_poster
from webapp import blogs as blog_store


# 1日に投稿する件数の上下限
# 最大は実質「上限なし」だが、暴走防止に50で打ち止め。
# 通常運用では news-worthiness スコア閾値で 5〜20 件に自然と収まる。
DEFAULT_MIN_ARTICLES = 3
DEFAULT_MAX_ARTICLES = 50

# トピック価値スコアの閾値
# 「質に振り切る」フェーズに移行 → 閾値を 6 → 8 に厳格化。
# 通る条件:
#  - 3メディア以上が報じる + ホットキーワード (6+2 = 8)
#  - 2メディア + ホットキーワード + 大手メディア (4+2+1 = 7) ← これは通さない
#  - 単発記事は完全に通さない
#  → 結果として「複数メディアで話題の本物のニュース」だけが採用される
_SCORE_PASS = 8


def run_for_blog(blog_id, dry_run=False):
    """1ブログ分の自動運用を実行。

    Returns:
        {
            'blog_id': str, 'blog_name': str,
            'results': [{topic, status, url?, issues_text?, reason?}, ...],
            'summary': '...',  # ログ用
        }
    """
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return {'blog_id': blog_id, 'blog_name': '(missing)', 'results': [], 'summary': 'blog not found'}

    print(f'[auto] start blog={blog_id} name="{blog.get("name")}"', flush=True)

    min_n = int(blog.get('auto_min_articles') or DEFAULT_MIN_ARTICLES)
    max_n = int(blog.get('auto_max_articles') or DEFAULT_MAX_ARTICLES)
    min_n = max(1, min(min_n, max_n))
    max_n = max(min_n, min(max_n, 20))

    topic_policy = (blog.get('topic_policy') or '').strip()

    # ---------- 1. トピック選定 ----------
    try:
        if topic_policy:
            # policy-mode: ニュース性スコアの概念がないので min_n 件固定
            topics = generate_policy_topics(topic_policy, count=min_n)
            print(f'[auto] policy-mode topics: {len(topics)}', flush=True)
        else:
            feeds = blog_store.feeds_for_blog(blog)
            if not feeds:
                return _empty_run(blog, 'ニュースソース未設定 (genre/RSS/topic_policy のいずれかが必要)')
            items = collect_recent_items(feeds, hours=72)
            posted = blog_store.get_posted_urls(blog_id)
            if posted:
                items = [it for it in items if it.get('link') not in posted]
            if not items:
                return _empty_run(blog, '新しい記事が無い (全て投稿済みURLと一致)')

            # 直近キーワードに時間減衰重みを付けて取得 (多様性ペナルティ用)
            # 「1か月は類似記事を出さない」方針: lookback=30 / decay=30
            recent_keyword_weights = {}
            try:
                recent_keyword_weights = blog_store.get_recent_keyword_weights(blog_id, decay_days=30, lookback_days=30)
                top = sorted(recent_keyword_weights.items(), key=lambda x: -x[1])[:6]
                print(f'[auto] recent_keyword_weights ({len(recent_keyword_weights)}): {top}', flush=True)
            except Exception as e:
                print(f'[auto] recent_keyword_weights failed (continuing): {e}', flush=True)

            # 過去30日のタイトル一覧を Gemini に渡して「これらと類似テーマを避ける」指示用
            recent_titles = []
            try:
                recent_titles = blog_store.get_recent_published_titles(blog_id, days=30)
                print(f'[auto] recent_titles (30d): {len(recent_titles)} entries', flush=True)
            except Exception as e:
                print(f'[auto] recent_titles failed (continuing): {e}', flush=True)

            # 多めに選んでからスコア付きで足切り (max_n の 3倍取得して、多様性で絞る)
            raw_topics = select_top_topics(
                items, count=max(max_n * 3, 10),
                genre=blog.get('genre', ''),
                niche_focus=blog.get('niche_focus', ''),
                exclude_topic_names=recent_titles,
            )
            topics = _rank_and_pick(raw_topics, min_n, max_n, recent_keyword_weights=recent_keyword_weights)
            print(f'[auto] news-mode topics chosen: {len(topics)} (from {len(raw_topics)} raw)', flush=True)
    except Exception as e:
        traceback.print_exc()
        return _empty_run(blog, f'トピック選定失敗: {e}')

    if not topics:
        return _empty_run(blog, 'トピック選定結果が0件')

    # ---------- 2. 各トピックを記事化 ----------
    # use_claude_writing=True なら Claude Code agent に本文執筆を委譲 (キューに積むのみ)
    use_claude = bool(blog.get('use_claude_writing'))
    results = []
    for i, topic in enumerate(topics, 1):
        print(f'[auto] -- topic {i}/{len(topics)}: {topic.get("name")} (claude={use_claude}) --', flush=True)
        try:
            if use_claude and not dry_run:
                r = _queue_topic_for_claude(blog, topic)
            else:
                r = _process_topic(blog, topic, dry_run=dry_run)
        except Exception as e:
            traceback.print_exc()
            r = {'topic': topic.get('name', ''), 'status': 'failed', 'reason': f'{type(e).__name__}: {e}'}
        results.append(r)

    # Threads は per-tick 投稿しない (スパム判定回避)。
    # 別途 Cloud Scheduler が 21:00 JST に /threads-daily-digest を叩いて
    # 1日1回まとめ投稿する。

    # ---------- 4. 通知 ----------
    try:
        notifier.send_digest(blog.get('name', blog_id), results)
    except Exception as e:
        print(f'[auto] notifier failed (continuing): {e}', flush=True)

    summary = _format_summary(results)
    print(f'[auto] done blog={blog_id} {summary}', flush=True)
    return {
        'blog_id': blog_id, 'blog_name': blog.get('name', ''),
        'results': results, 'summary': summary,
    }


# ---------- topic ranking ----------

_HOT_KEYWORDS = (
    '発表', '発売', '新型', '新モデル', '正式', '公開', '対応', '搭載',
    'リリース', 'ローンチ', '更新', 'アップデート', '値下げ', '値上げ',
    'launch', 'reveal', 'announce', 'release', 'unveil', 'debut',
)

# 「新ネタ性」を示すマーカー — 過去キーワードと被ってもこれがあれば実質ペナルティ無効
# (後継モデル/更新ニュース/リーク/値段変動などは "別記事として書く価値あり" 扱い)
_NOVELTY_MARKERS = (
    # 後継/新世代系
    '新型', '新モデル', '新作', '後継', '次世代', '次期',
    '第2世代', '第3世代', '第4世代', '第5世代', '第6世代',
    '2世代', '3世代', '4世代', '5世代',
    # 更新/変更系
    'アップデート', 'update', 'アプデ', '改定', '改訂', '改正',
    '値下げ', '値上げ', '価格改定', 'セール',
    'リーク', 'leak', 'リコール', 'recall',
    '不具合', '修正', 'パッチ', 'セキュリティ', '脆弱性', 'バージョン',
    # 年号(更新ニュースのシグナル)
    '2026年', '2027年', '今年版', '最新版',
)


def _has_novelty_marker(text):
    """テキストに新ネタ性マーカーが含まれるか (=過去被りでも別ネタ扱い)。"""
    lower = (text or '').lower()
    return any(m.lower() in lower for m in _NOVELTY_MARKERS)


# トピック出典の言語圏判定 (ニッチ枠の海外スクープ優先用)
# ドメイン名 + Google News から拾えるパブリッシャー名の両方を含む
_JAPANESE_HINTS = (
    # ドメイン
    'itmedia.co.jp', 'ascii.jp', 'gizmodo.jp', 'impress.co.jp',
    'pc.watch', 'gigazine.net', '4gamer.net', 'famitsu.com',
    'kakaku.com', 'mynavi.jp', 'wired.jp', 'getnews.jp',
    'iphone-mania.jp', 'gori.me', 'ipodwave.com', 'phileweb.com',
    'sumahosupportline.com',
    # パブリッシャー名 (Google News の <source> から拾える表記)
    'itmedia', 'ascii.jp', 'gizmodo japan', 'impress watch',
    'pc watch', 'gigazine', 'ファミ通', '4gamer', 'マイナビ',
    'iphone mania', 'engadget日本版', 'wired.jp', 'gori.me',
    'ねとらぼ', 'k-tai watch', 'av watch', 'pc-koubou',
)

_FOREIGN_HINTS = (
    # ドメイン
    'theverge.com', 'engadget.com', 'techcrunch.com', 'arstechnica.com',
    '9to5mac.com', '9to5google.com', 'macrumors.com', 'androidauthority.com',
    'gsmarena.com', 'tomshardware.com', 'wired.com', 'theinformation.com',
    'reuters.com', 'bloomberg.com', 'cnet.com', 'androidpolice.com',
    'androidcentral.com', 'cultofmac.com', 'theregister.com',
    'tomsguide.com', 'pcmag.com', 'digitaltrends.com', 'gizmodo.com',
    # パブリッシャー名 (Google News の <source> 表記)
    'the verge', '9to5mac', '9to5google', 'macrumors', 'engadget',
    'techcrunch', 'ars technica', 'gsmarena', 'android authority',
    'tom\'s guide', 'tom\'s hardware', 'pcmag', 'cnet',
    'android police', 'android central', 'cult of mac',
    'wired', 'reuters', 'bloomberg', 'the information',
    'digital trends', 'gizmodo', 'forbes',
)


def _topic_origin(topic):
    """トピックを構成するソースの言語圏を判定。

    Returns:
        'foreign_only' — 海外メディアのみ (= 日本未報道の可能性高、ニッチ枠の最優先候補)
        'has_jp' — 日本のメディアが含まれている
        'unknown' — Google News で <source> が取れなかった等で判定不可
    """
    related = topic.get('related') or []
    has_jp = False
    has_foreign = False
    for r in related:
        blob = ((r.get('source') or '') + ' ' + (r.get('link') or '')).lower()
        if any(h in blob for h in _FOREIGN_HINTS):
            has_foreign = True
        if any(h in blob for h in _JAPANESE_HINTS):
            has_jp = True
    if has_foreign and not has_jp:
        return 'foreign_only'
    if has_jp:
        return 'has_jp'
    return 'unknown'


def _topic_score(topic, recent_keyword_weights=None):
    """ニュース価値を 0〜10 で粗くスコア化 + 多様性ペナルティ (強化版)。

    recent_keyword_weights: dict {keyword: float_weight}
      時間減衰重み付き。新しい記事ほど重み大、古い記事はほぼ0。

    ペナルティ強化点:
    - 上限を -4 → -7 に引き上げ (1日後の重複は確実にブロック)
    - 長い固有名詞 (4文字以上) は2倍重く扱う (Googlebook 等の即時連投を防ぐ)
    - 新ネタ性マーカーがあっても緩和は 0.4 → 0.5 (やや控えめに)
    """
    related = topic.get('related') or []
    cluster_size = len(related)
    # 複数メディア報道 = 最重要シグナル
    score = min(cluster_size * 2, 6)
    name = topic.get('name', '')
    summary = topic.get('summary', '')
    blob_lower = f'{name} {summary}'.lower()
    if any(k in blob_lower for k in _HOT_KEYWORDS):
        score += 2
    # 海外大手メディア (The Verge等) が含まれているとボーナス
    big_sources = ('the verge', '9to5', 'macrumors', 'engadget', 'techcrunch', 'arstechnica', 'itmedia')
    for r in related:
        src = (r.get('source') or '').lower()
        if any(b in src for b in big_sources):
            score += 1
            break

    # 多様性ペナルティ (時間減衰 + マーカー考慮 + 長い固有名詞は重く)
    # 「1か月は類似記事を出さない」方針: ペナルティ上限を 7 → 12 に強化
    if recent_keyword_weights:
        overlap_weight = 0.0
        for kw, w in recent_keyword_weights.items():
            if kw and len(kw) >= 2 and kw in blob_lower:
                # 4文字以上の固有名詞 (製品名等) は3倍重く扱う(以前は2倍)
                multiplier = 3.0 if len(kw) >= 4 else 1.5
                overlap_weight += w * multiplier

        if overlap_weight > 0:
            adjust = 1.0
            if _has_novelty_marker(blob_lower):
                adjust *= 0.6  # 新ネタ性マーカーがあっても緩和は控えめ(0.5→0.6)

            penalty = overlap_weight * adjust
            # 上限を 7 → 12 に引き上げ (重複ブロック大幅強化、最大スコア10を下回ることもあり得る)
            penalty = min(penalty, 12.0)
            score -= int(round(penalty))

    return max(0, min(score, 10))


def _rank_and_pick(raw_topics, min_n, max_n, recent_keyword_weights=None):
    """話題枠 (max_n - 1) + ニッチ枠 (1) のミックスで候補を返す。

    max_n >= 3 の時は最後の1枠を「ニッチ枠」(=複数メディア非報道だが
    独自視点のあるテーマ) に充てる。SEO的に競合が薄いキーワードを
    取れるので長期的な伸びに寄与する。

    枠の定義:
      - 話題枠: cluster_size >= 2 を優先、score >= _SCORE_PASS(6)
      - ニッチ枠: cluster_size <= 2 でも score >= 3 で採用 (低めの閾値)
    """
    scored = [(t, _topic_score(t, recent_keyword_weights)) for t in raw_topics]
    scored.sort(key=lambda x: -x[1])

    # max_n < 2 は単純ロジック (枠割り当て不要)
    if max_n < 2:
        picks = []
        for t, s in scored:
            if len(picks) >= max_n:
                break
            if s >= _SCORE_PASS:
                picks.append(t)
            elif len(picks) < min_n:
                picks.append(t)
        return picks

    # max_n >= 2: 話題 (max_n - 1) + ニッチ (1) の枠割り当て
    # max_n=2 → 話題1 + ニッチ1
    # max_n=3 → 話題2 + ニッチ1
    trending_quota = max_n - 1
    niche_quota = 1

    trending_picks = []
    niche_picks = []
    used_names = set()

    def _name(t):
        return t.get('name', '')

    # ステップ1: 話題枠を 「cluster_size >= 2 かつ score >= 6」 で埋める
    for t, s in scored:
        if len(trending_picks) >= trending_quota:
            break
        if _name(t) in used_names:
            continue
        cluster_size = len(t.get('related') or [])
        if cluster_size >= 2 and s >= _SCORE_PASS:
            trending_picks.append(t)
            used_names.add(_name(t))

    # ステップ2: 話題枠に空き → score >= 6 ならcluster_sizeを問わず追加
    if len(trending_picks) < trending_quota:
        for t, s in scored:
            if len(trending_picks) >= trending_quota:
                break
            if _name(t) in used_names:
                continue
            if s >= _SCORE_PASS:
                trending_picks.append(t)
                used_names.add(_name(t))

    # ステップ3a: ニッチ枠 — 「海外メディアのみ報道 = 日本未報道」を最優先
    for t, s in scored:
        if len(niche_picks) >= niche_quota:
            break
        if _name(t) in used_names:
            continue
        if _topic_origin(t) != 'foreign_only':
            continue
        if s >= 3:
            niche_picks.append(t)
            used_names.add(_name(t))
            print(f'[auto] niche-foreign: {_name(t)[:50]} (score={s})', flush=True)

    # ステップ3b: 海外スクープが無ければ、cluster <= 2 の通常ニッチ
    if len(niche_picks) < niche_quota:
        for t, s in scored:
            if len(niche_picks) >= niche_quota:
                break
            if _name(t) in used_names:
                continue
            cluster_size = len(t.get('related') or [])
            if cluster_size <= 2 and s >= 3:
                niche_picks.append(t)
                used_names.add(_name(t))
                print(f'[auto] niche-fallback: {_name(t)[:50]} (cluster={cluster_size}, score={s})', flush=True)

    picks = trending_picks + niche_picks

    # min_n に満たない場合の保険 (policy-mode用)
    if len(picks) < min_n:
        for t, s in scored:
            if len(picks) >= min_n:
                break
            if _name(t) in used_names:
                continue
            picks.append(t)
            used_names.add(_name(t))

    print(f'[auto] picks: trending={len(trending_picks)} niche={len(niche_picks)}', flush=True)
    return picks


# ---------- per-topic processing ----------

def _prepare_topic_context(blog, topic):
    """ライティング前の準備工程 (画像取得・関連記事・プロンプト組み立て) をまとめて返す。

    返り値の dict は _finalize_topic に渡してパイプライン後段の処理に使う。
    また、Claude Code agent に投げる際の prompt + context として serialize 可能。
    """
    from src.article_generator import build_article_prompt
    blog_id = blog['id']
    topic_name = topic.get('name', '')
    topic_summary = topic.get('summary', '')

    # 参考記事取得
    if topic.get('related'):
        sources = fetch_article_texts(topic['related'])
    else:
        sources = []
    if not sources:
        sources = [{
            'title': topic_name, 'url': '',
            'source': 'Gemini知識ベース',
            'text': f'「{topic_name}」について、あなたが持つ知識をもとに記事を書いてください。\nテーマの概要: {topic_summary}',
            'image': None,
        }]

    # 画像 (Wiki検索ワードを Gemini で抽出してヒット率UP)
    wiki_images = None
    if blog.get('use_wiki_images'):
        try:
            wiki_images = find_images_smart(topic_name, topic_summary, count=2) or None
        except Exception as e:
            print(f'[auto] wiki_images failed: {e}', flush=True)
    official_image = None
    if blog.get('vocab', {}).get('feature_official_image', True):
        try:
            official_image = resolve_official_image(topic_name, sources)
        except Exception as e:
            print(f'[auto] official_image failed: {e}', flush=True)

    # 関連過去記事 (内部リンク) — 下書き/削除済みを除外するため HEAD-check
    related_articles = []
    try:
        candidates = blog_store.find_related_articles(blog_id, topic_name, topic_summary, limit=8)
        # 公開済み(2xx/3xx)のみフィルター
        import requests as _req
        for c in candidates:
            url = c.get('url', '')
            if not url:
                continue
            try:
                r = _req.head(url, timeout=6, allow_redirects=True,
                              headers={'User-Agent': 'Mozilla/5.0 (LinkChecker)'})
                # Hatena は HEAD で 404 を返すことがあるので念のため GET 再確認
                if r.status_code == 404:
                    g = _req.get(url, timeout=6, allow_redirects=True, stream=True,
                                 headers={'User-Agent': 'Mozilla/5.0 (LinkChecker)'})
                    g.close()
                    alive = 200 <= g.status_code < 400
                else:
                    alive = 200 <= r.status_code < 400
            except Exception:
                alive = False
            if alive:
                related_articles.append(c)
            if len(related_articles) >= 4:
                break
        print(f'[auto] related (live): {len(related_articles)} / {len(candidates)} 件', flush=True)
    except Exception as e:
        print(f'[auto] find_related failed: {e}', flush=True)

    # ロングテールキーワード (検索ニッチ取り)
    longtail_keywords = None
    try:
        longtail_keywords = suggest_longtail(topic_name)
        if longtail_keywords:
            print(f'[auto] longtail: {len(longtail_keywords)}件 [{longtail_keywords[0].get("theme","")[:40]}…]', flush=True)
    except Exception as e:
        print(f'[auto] longtail failed (continuing): {e}', flush=True)

    # 楽天API資格情報があれば、Amazon/楽天テキストリンクではなく実商品カードを使う
    use_product_cards = bool(
        (blog.get('rakuten_app_id') or '').strip()
        and (blog.get('rakuten_access_key') or '').strip()
    )

    # フルプロンプトを組み立て (Gemini にも Claude agent にも同じプロンプトを渡せる)
    prompt, article_type = build_article_prompt(
        topic_name, topic_summary, sources,
        tone_prompt=blog.get('tone_prompt', ''),
        genre=blog.get('genre') or 'ブログ',
        amazon_affiliate_tag=blog.get('amazon_affiliate_tag', ''),
        rakuten_affiliate_id=blog.get('rakuten_affiliate_id', ''),
        article_policy=blog.get('article_policy', ''),
        use_charts=blog.get('use_charts', False),
        wiki_images=wiki_images,
        official_image=official_image,
        ownership='not_owned',
        related_articles=related_articles,
        longtail_keywords=longtail_keywords,
        use_product_cards=use_product_cards,
    )

    source_urls = [s.get('url') for s in sources if s.get('url')]

    return {
        'blog_id': blog_id,
        'topic_name': topic_name,
        'topic_summary': topic_summary,
        'sources': sources,
        'source_urls': source_urls,
        'wiki_images': wiki_images,
        'official_image': official_image,
        'related_articles': related_articles,
        'longtail_keywords': longtail_keywords,
        'use_product_cards': use_product_cards,
        'article_type': article_type,
        'prompt': prompt,
    }


def _process_topic(blog, topic, dry_run=False):
    """1トピックを記事化 → コンプラ検査 → 投稿 or 下書き保存 (同期/Gemini版)。"""
    ctx = _prepare_topic_context(blog, topic)
    topic_name = ctx['topic_name']
    topic_summary = ctx['topic_summary']

    # 生成 (Gemini)
    title, body = generate_article(
        topic_name, topic_summary, ctx['sources'],
        tone_prompt=blog.get('tone_prompt', ''),
        genre=blog.get('genre') or 'ブログ',
        amazon_affiliate_tag=blog.get('amazon_affiliate_tag', ''),
        rakuten_affiliate_id=blog.get('rakuten_affiliate_id', ''),
        article_policy=blog.get('article_policy', ''),
        use_charts=blog.get('use_charts', False),
        wiki_images=ctx['wiki_images'],
        official_image=ctx['official_image'],
        ownership='not_owned',
        related_articles=ctx['related_articles'],
        longtail_keywords=ctx['longtail_keywords'],
        use_product_cards=ctx['use_product_cards'],
    )

    return _finalize_topic(blog, ctx, title, body, dry_run=dry_run)


def _finalize_topic(blog, ctx, title, body, dry_run=False):
    """書き終えた本文を受け取り、後段の処理(品質スコア→ピラー化→カード→投稿→Threads)を実行。

    Gemini 版でも Claude agent 経由でも、本文ができた後はこの関数に渡せばOK。
    """
    blog_id = ctx['blog_id']
    topic_name = ctx['topic_name']
    topic_summary = ctx['topic_summary']
    sources = ctx['sources']
    related_articles = ctx['related_articles'] or []
    longtail_keywords = ctx['longtail_keywords']
    use_product_cards = ctx['use_product_cards']
    source_urls = ctx['source_urls']

    # 品質スコア → 低ければ1回だけ自動改善
    quality = score_article(title, body, ownership='not_owned')
    if quality['total'] < 65:
        instructions = build_improvement_instructions(quality)
        if instructions:
            try:
                title, body = revise_article(
                    title, body, instructions,
                    tone_prompt=blog.get('tone_prompt', ''),
                    article_policy=blog.get('article_policy', ''),
                    ownership='not_owned',
                )
                quality = score_article(title, body, ownership='not_owned')
                print(f'[auto] auto-improved: score {quality["total"]}', flush=True)
            except Exception as e:
                print(f'[auto] auto-improve failed (continuing): {e}', flush=True)

    # v2 ポリシー: 3000-3800字で完結させるため pillarize は廃止
    # (旧: pillarize で 5500未満を加筆 → 字数膨張の原因だった)
    print(f'[auto] v2 mode: pillarize skipped (target 3000-3800 chars, actual {len(body)})', flush=True)

    # [PRODUCT_CARD: 商品名] プレースホルダーを楽天API実商品カードHTMLに置換
    # (ピラー化後・rehost前にやることで、カード画像も Fotolife へ rehost される)
    if use_product_cards:
        # まず Claude が書いたプレースホルダを実カードに置換
        try:
            body = product_search.replace_placeholders(body, blog)
        except Exception as e:
            print(f'[auto] product-card replace failed (continuing): {e}', flush=True)

        # 置換後に実際に何枚カードが入ったか確認 (楽天で検索ヒットしなかったものは消えてる)
        # カードHTMLの判別マーカー = "楽天市場価格" は build_card_html 内で必ず出る
        card_count = body.count('楽天市場価格')
        if card_count < 2:
            print(f'[auto] WARN: only {card_count} actual product cards rendered. '
                  f'Topic: "{topic_name[:40]}"', flush=True)
            # 鉄板商品リスト (楽天で確実にヒットする型番)
            niche_focus = (blog.get('niche_focus') or '').lower()
            if 'ar' in niche_focus or 'vr' in niche_focus or '空間' in niche_focus or 'spatial' in niche_focus:
                default_keywords = ['XREAL One', 'Meta Quest 3', 'VITURE One Lite', 'Apple Vision Pro', 'Bose QuietComfort Ultra']
            else:
                default_keywords = ['Anker PowerCore 10000', 'Apple AirTag', 'ロジクール MX Master 3S', 'Anker PowerCore Magnetic 5K', 'Bose QuietComfort Ultra']
            needed = max(0, 2 - card_count)
            # v2 ポリシー: H2 を増やさないよう、新規セクション見出しは作らない。
            # 既存の「## まとめ」の直前に短い誘導文+カードだけ静かに足す。
            picks = default_keywords[:needed + 2]
            fallback_text = '\n\n気になる人は型番と実勢価格を下のカードから確認しておくと早い。\n\n'
            for kw in picks:
                fallback_text += f'[PRODUCT_CARD: {kw}]\n\n'
            # 「## まとめ」「## よくある質問」より前に挿入 (H2見出し追加なし)
            inserted = False
            for marker in ('## まとめ', '## よくある質問', '## 情報ソース'):
                if marker in body:
                    body = body.replace(marker, fallback_text + marker, 1)
                    inserted = True
                    break
            if not inserted:
                body = body.rstrip() + fallback_text
            # 再度プレースホルダ置換 (鉄板商品のカードを実際に取得)
            try:
                body = product_search.replace_placeholders(body, blog)
            except Exception as e:
                print(f'[auto] fallback product-card replace failed: {e}', flush=True)
            new_card_count = body.count('楽天市場価格')
            print(f'[auto] injected fallback {len(picks)} placeholders, '
                  f'rendered: {card_count} -> {new_card_count} actual cards', flush=True)

    # 内部リンク CTA ブロック (「次に読むならこちら」+ ジャンル別ハブ誘導)
    # rehost より前で挿入。CTA 内に画像はないが将来サムネ追加時のため。
    try:
        from src import internal_cta
        from src import hub_generator as _hub
        hub_article = _hub.find_matching_hub(blog_id, topic_name, title)
        body = internal_cta.inject_into_body(body, related_articles, hub_article)
        if hub_article:
            print(f'[auto] CTA: related={len(related_articles)} + hub={hub_article.get("title","")[:30]}', flush=True)
        else:
            print(f'[auto] CTA: related={len(related_articles)} (no hub matched)', flush=True)
    except Exception as e:
        print(f'[auto] CTA inject failed (continuing): {e}', flush=True)

    # 画像 rehost (外部URL → Fotolife)
    rehost_removed = 0
    try:
        body, stats = rehost_external_images(blog, body)
        rehost_removed = stats.get('removed', 0)
        print(f'[auto] rehost: +{stats.get("rehosted",0)} / -{stats.get("removed",0)}', flush=True)
    except Exception as e:
        print(f'[auto] rehost failed (continuing): {e}', flush=True)

    # コンプラ自己検査 (公開ブロックには使わない、ログ&Warn通知のみ)
    check = check_article(title, body, sources=sources, rehost_removed=rehost_removed)
    print(f'[auto] compliance: {check["summary"]}', flush=True)

    source_urls = [s.get('url') for s in sources if s.get('url')]

    # 公開ブロック機能は完全廃止。コンプラBLOCK があってもログだけ残して強行公開する。
    if not check['ok']:
        block_kinds = ','.join(b.get('kind', '?') for b in check.get('blocks', []))
        print(f'[auto] compliance BLOCKs ignored, publishing anyway: {block_kinds}', flush=True)
    print(f'[auto] quality score={quality.get("total")} (publish-through enabled)', flush=True)

    # 投稿
    if dry_run:
        return {
            'topic': topic_name, 'status': 'published', 'url': '(dry-run)',
            'quality_score': quality.get('total'),
            'title': title,
            'body_preview': body[:3000],  # 検証用 (本文の先頭3000字)
            'body_length': len(body),
        }

    # はてなブログのグループバナー (ランキング反映用) を記事末尾に挿入
    banners = (blog.get('hatena_group_banners') or '').strip()
    if banners:
        body = body.rstrip() + '\n\n' + banners + '\n'

    site_url = f'https://{blog["hatena_blog_domain"]}' if blog.get('hatena_blog_domain') else ''
    try:
        jsonld = build_jsonld(title, body, kind='article',
                              author_name=blog.get('hatena_id', ''), site_url=site_url)
        body_with_schema = append_jsonld(body, jsonld)
    except Exception as e:
        print(f'[auto] jsonld failed (continuing): {e}', flush=True)
        body_with_schema = body

    # カテゴリ自動推測 (Geminiで記事内容から3〜5個)
    auto_cats = []
    try:
        auto_cats = suggest_categories(title, body)
        print(f'[auto] categories: {auto_cats}', flush=True)
    except Exception as e:
        print(f'[auto] categorizer failed (continuing): {e}', flush=True)

    # 下書きとして保存 (公開はユーザーが Hatena 管理画面で手動実行)
    url = publish(blog, title, body_with_schema, extra_categories=auto_cats, draft=True)
    print(f'[auto] saved as DRAFT: {url}', flush=True)

    # 記録 (ソースURL重複防止 + 過去記事インデックス)
    try:
        blog_store.record_posted_urls(blog_id, source_urls)
    except Exception as e:
        print(f'[auto] record_posted_urls failed: {e}', flush=True)
    try:
        blog_store.record_published_article(blog_id, title, body, url)
    except Exception as e:
        print(f'[auto] record_published_article failed: {e}', flush=True)

    # IndexNow / Google Indexing / X / Threads は 公開時 のみ通知/投稿。
    # 下書き保存の段階では URL が public でないので、これらの通知は行わない。
    # 公開はユーザーが Hatena 管理画面で手動実行 → その時点で必要なら手動通知する設計。
    print('[auto] skipped IndexNow/Indexing/Threads notifications (draft mode)', flush=True)

    kw_hints = [topic_name]
    if longtail_keywords:
        first_lt = longtail_keywords[0]
        kw_hints.append(first_lt.get('theme') if isinstance(first_lt, dict) else str(first_lt))

    warns_text = ''
    if check['warns']:
        warns_text = format_issues_for_human(check)

    return {
        'topic': topic_name, 'status': 'drafted',
        'url': url, 'quality_score': quality.get('total'),
        'warns_text': warns_text or None,
        'title': title,
        'summary': topic_summary,
        'keywords': kw_hints,
    }


# ---------- Claude Code agent 連携 (writing-only 切り出し) ----------

def _queue_topic_for_claude(blog, topic):
    """トピックを準備して pending_writes キューに保存。本文執筆は Claude Code agent に委譲。
    Returns: {'topic','status','doc_id'}
    """
    from src import article_queue
    ctx = _prepare_topic_context(blog, topic)
    # context は finalize で必要だが、Firestore に保存できない object (datetime 内含む) があるとNG
    # source-image 等はシリアライズ可能な dict のみ含む
    serializable_ctx = {
        'blog_id': ctx['blog_id'],
        'topic_name': ctx['topic_name'],
        'topic_summary': ctx['topic_summary'],
        'sources': ctx['sources'],
        'source_urls': ctx['source_urls'],
        'wiki_images': ctx['wiki_images'],
        'official_image': ctx['official_image'],
        'related_articles': ctx['related_articles'],
        'longtail_keywords': ctx['longtail_keywords'],
        'use_product_cards': ctx['use_product_cards'],
        'article_type': ctx['article_type'],
    }
    doc_id = article_queue.save_pending(
        ctx['blog_id'], ctx['topic_name'], ctx['topic_summary'],
        ctx['prompt'], serializable_ctx,
    )
    print(f'[auto] queued for Claude: {ctx["topic_name"][:40]} (doc={doc_id})', flush=True)
    return {'topic': ctx['topic_name'], 'status': 'queued', 'doc_id': doc_id}


def finalize_from_queue(doc_id, title, body, dry_run=False):
    """Claude Code agent (または fallback) が書き終えた本文を受け取って後段処理。
    /admin/submit-written-body から呼ばれる。
    """
    from src import article_queue
    pending = article_queue.get_pending(doc_id)
    if not pending:
        return {'ok': False, 'error': 'doc_not_found'}
    if pending['status'] not in ('writing', 'pending'):  # pending も許容 (fallback の場合)
        return {'ok': False, 'error': f'invalid_status: {pending["status"]}'}

    blog = blog_store.get_blog(pending['blog_id'])
    if not blog:
        article_queue.mark_failed(doc_id, 'blog_not_found')
        return {'ok': False, 'error': 'blog_not_found'}

    ctx = pending.get('context') or {}
    # 一部のフィールド型 (firestore は dict ではなく内部型を返す場合があるので安全化)
    ctx = dict(ctx)
    try:
        result = _finalize_topic(blog, ctx, title, body, dry_run=dry_run)
    except Exception as e:
        traceback.print_exc()
        article_queue.mark_failed(doc_id, e)
        return {'ok': False, 'error': str(e)}

    if result.get('status') == 'published':
        article_queue.mark_published(doc_id, result.get('url', ''), result.get('title') or title)
    return {'ok': True, **result}


def run_claude_fallback(stale_minutes=30, limit=3):
    """30分以上 pending or writing のままの記事を Gemini で書いて公開する保険cron。
    Returns: {'processed': N, 'results': [...]}
    """
    from src import article_queue
    stale = article_queue.list_stale_pending(stale_minutes=stale_minutes)
    if not stale:
        return {'processed': 0, 'results': []}

    results = []
    for doc in stale[:limit]:
        doc_id = doc['doc_id']
        blog = blog_store.get_blog(doc['blog_id'])
        if not blog:
            article_queue.mark_failed(doc_id, 'blog_not_found_fallback')
            continue
        ctx = doc.get('context') or {}
        prompt = doc.get('prompt') or ''

        # Gemini で書く (article_generator の内部 helper を直接呼ぶ)
        try:
            from src.article_generator import _generate_with_search, _split_title, _strip_code_fence
            from google import genai
            from config import GEMINI_API_KEY
            client = genai.Client(api_key=GEMINI_API_KEY)
            md = _generate_with_search(client, prompt)
            if md.startswith('```'):
                md = _strip_code_fence(md)
            title, body = _split_title(md)
            print(f'[fallback] {doc_id} written by Gemini ({len(body)} chars)', flush=True)
        except Exception as e:
            traceback.print_exc()
            article_queue.mark_failed(doc_id, f'gemini_fallback: {e}')
            results.append({'doc_id': doc_id, 'ok': False, 'error': str(e)})
            continue

        # finalize
        try:
            result = _finalize_topic(blog, dict(ctx), title, body)
            if result.get('status') == 'published':
                # mark with special status to distinguish from Claude success
                article_queue._client().collection(article_queue._COLL).document(doc_id).update({
                    'status': 'fallback_published',
                    'finished_at': datetime.now(timezone.utc),
                    'published_url': result.get('url'),
                    'final_title': result.get('title') or title,
                })
            results.append({'doc_id': doc_id, 'ok': True, **result})
        except Exception as e:
            traceback.print_exc()
            article_queue.mark_failed(doc_id, e)
            results.append({'doc_id': doc_id, 'ok': False, 'error': str(e)})

    return {'processed': len(results), 'results': results}


# ---------- helpers ----------

def _empty_run(blog, reason):
    print(f'[auto] {blog.get("id")} empty run: {reason}', flush=True)
    return {
        'blog_id': blog.get('id'),
        'blog_name': blog.get('name', ''),
        'results': [],
        'summary': reason,
    }


def _format_summary(results):
    pub = sum(1 for r in results if r['status'] == 'published')
    draft = sum(1 for r in results if r['status'] == 'drafted')
    fail = sum(1 for r in results if r['status'] == 'failed')
    return f'published={pub} drafted={draft} failed={fail}'
