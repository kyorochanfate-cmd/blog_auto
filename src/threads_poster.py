"""Threads 自動投稿。

記事公開直後に Threads API (Graph API) で告知投稿する。

認証: 長期間ユーザーアクセストークン (Threads Tester から生成、60日有効)
ブログごとに認証情報を保存するので複数アカウント使い分けOK。

レート制限 (Threads Free):
- 250投稿/24時間 (Xの50より緩い)
- 月7,500投稿まで無料

トークンは60日で切れるので、別途 threads_token_refresh.py で自動延長する。

投稿モード:
- post_article(): 単発投稿 (1記事 → 1スレッド)
- post_chain(): スレッドチェーン (N記事 → 1メイン + N-1リプライ)
  → 連投スパム判定を回避しつつ複数記事を告知できる
"""
import re
import time
import requests


DEFAULT_TEMPLATE = '{title}\n\n{url}\n\n{hashtags}'

# Threads の本文上限は 500文字
_TEXT_MAX = 480


def is_configured(blog):
    return bool(blog.get('threads_access_token') and blog.get('threads_user_id'))


def post_article(blog, title, url, summary='', keywords=None):
    """記事を Threads に告知投稿。

    Returns:
        {'ok': bool, 'thread_id'?: str, 'text'?: str, 'error'?: str, 'skipped'?: str}
    例外は投げない (記事公開フローを止めないため)。
    """
    if not blog.get('threads_auto_post_enabled'):
        return {'skipped': 'disabled'}
    if not is_configured(blog):
        return {'skipped': 'credentials_missing'}
    if not (title and url):
        return {'skipped': 'no_title_or_url'}

    template = (blog.get('threads_template') or '').strip() or DEFAULT_TEMPLATE
    hashtags = _build_hashtags(blog, keywords)
    text = (
        template
        .replace('{title}', _clip(title, 200))
        .replace('{url}', url)
        .replace('{summary}', _clip(summary or '', 150))
        .replace('{hashtags}', hashtags)
    )
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX - 1] + '…'

    token = blog['threads_access_token']
    user_id = blog['threads_user_id']
    base = f'https://graph.threads.net/v1.0/{user_id}'

    # Step 1: Create media container (link_attachment で画像プレビュー必須化)
    try:
        r1 = requests.post(f'{base}/threads', data={
            'media_type': 'TEXT',
            'text': text,
            'link_attachment': url,  # ★必須★ リンクカード(画像付きプレビュー)を表示
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'container_request_exception: {e}'}

    if r1.status_code >= 400:
        return {'ok': False, 'error': f'container_failed HTTP {r1.status_code}: {r1.text[:300]}'}
    creation_id = (r1.json() or {}).get('id')
    if not creation_id:
        return {'ok': False, 'error': f'no_creation_id: {r1.text[:200]}'}

    time.sleep(2)  # ベストプラクティス: container 完成を待つ

    # Step 2: Publish container
    try:
        r2 = requests.post(f'{base}/threads_publish', data={
            'creation_id': creation_id,
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'publish_request_exception: {e}'}

    if r2.status_code >= 400:
        return {'ok': False, 'error': f'publish_failed HTTP {r2.status_code}: {r2.text[:300]}'}

    thread_id = (r2.json() or {}).get('id')
    print(f'[threads] OK id={thread_id} text={text[:60]!r}', flush=True)
    return {'ok': True, 'thread_id': thread_id, 'text': text}


def post_chain(blog, articles):
    """N記事を「1メイン + N-1リプライ」のスレッドチェーンで投稿する。

    Args:
        blog: ブログ設定 dict
        articles: [{'title','url','summary','keywords'}, ...]
                  N=1 なら通常の post_article として動作。

    Returns:
        {'count': N_posted, 'posted': [{'index','thread_id'}, ...], 'error'?: str}
    """
    if not articles:
        return {'skipped': 'no_articles'}
    if not blog.get('threads_auto_post_enabled'):
        return {'skipped': 'disabled'}
    if not is_configured(blog):
        return {'skipped': 'credentials_missing'}

    # 1記事だけなら単発投稿に委譲
    if len(articles) == 1:
        a = articles[0]
        r = post_article(
            blog, a.get('title', ''), a.get('url', ''),
            summary=a.get('summary', ''), keywords=a.get('keywords', []),
        )
        return {'count': 1 if r.get('ok') else 0, 'posted': [r]}

    token = blog['threads_access_token']
    user_id = blog['threads_user_id']
    base = f'https://graph.threads.net/v1.0/{user_id}'

    posted = []
    parent_id = None
    last_error = None

    for i, art in enumerate(articles):
        text = _compose_chain_text(blog, articles, i, art)
        url = (art.get('url') or '').strip()
        if not (text and url):
            continue

        params = {
            'media_type': 'TEXT',
            'text': text,
            'link_attachment': url,  # 各リプライにも個別のリンクカード
            'access_token': token,
        }
        if parent_id:
            params['reply_to_id'] = parent_id

        try:
            r1 = requests.post(f'{base}/threads', data=params, timeout=20)
        except Exception as e:
            last_error = f'chain[{i}] container_request: {e}'
            break
        if r1.status_code >= 400:
            last_error = f'chain[{i}] container HTTP {r1.status_code}: {r1.text[:200]}'
            print(f'[threads] {last_error}', flush=True)
            break
        creation_id = (r1.json() or {}).get('id')
        if not creation_id:
            last_error = f'chain[{i}] no creation_id'
            break

        time.sleep(2)

        try:
            r2 = requests.post(f'{base}/threads_publish', data={
                'creation_id': creation_id,
                'access_token': token,
            }, timeout=20)
        except Exception as e:
            last_error = f'chain[{i}] publish_request: {e}'
            break
        if r2.status_code >= 400:
            last_error = f'chain[{i}] publish HTTP {r2.status_code}: {r2.text[:200]}'
            print(f'[threads] {last_error}', flush=True)
            break

        thread_id = (r2.json() or {}).get('id')
        posted.append({'index': i, 'thread_id': thread_id})
        parent_id = thread_id
        print(f'[threads] chain[{i}] OK id={thread_id}', flush=True)

        # 次のリプライまで間隔を空ける (スパム判定回避)
        if i < len(articles) - 1:
            time.sleep(8)

    result = {'count': len(posted), 'posted': posted}
    if last_error:
        result['error'] = last_error
    return result


_DAILY_DIGEST_MAIN_PROMPT = """以下は今日(過去24時間)あなたが運営する {genre} ブログで公開した記事のタイトル一覧です。
これを踏まえて、Threads(SNS)のメイン投稿用に、**ブログ運営者が一日を振り返るような自然な独り言コメント**を1つ書いてください。

【今日公開した記事タイトル】
{titles}

【絶対ルール — 違反は重大なスパム判定リスク】
- 100〜180文字
- **URLや記事タイトルそのものは入れない**(リンクと記事はリプライで別途記載する)
- **ハッシュタグは禁止**(スパムっぽくなるため)
- **絵文字禁止**
- 「ブログを読んでください」「リンクで詳細」「フォローしてください」などの宣伝文句は厳禁
- 押し付けがましくない、自然な独り言調(「最近〜の動きが活発」「今日は〇〇関連の話題が多かった」など)
- 業界観察 or 個人的な印象を述べる感じ
- 「と感じる」「と思う」「気になる」など主観マーカーで人間らしく

出力は本文のみ。前置き・コードフェンス禁止。"""


def post_daily_digest(blog, articles, max_replies=3, dry_run=False):
    """1日1回の Threads ダイジェスト投稿(スパム回避特化版)。

    動作:
      1. メイン投稿: Gemini が業界振り返り風の独り言を生成(リンクなし・ハッシュタグなし)
      2. リプライ1〜3本: 個別記事 + URL(link_attachment) を 60〜120秒間隔で投稿

    Args:
        blog: Firestore のブログ設定 dict
        articles: [{'title','url','keywords','published_at'}, ...] 新しい順
        max_replies: 最大リプライ数 (default 3)
        dry_run: True なら投稿せず生成テキストだけ返す

    Returns:
        {'ok': bool, 'main_thread_id'?, 'replies': [...], 'count': N, 'error'?: str}
    """
    if not articles:
        return {'skipped': 'no_articles'}
    if not blog.get('threads_auto_post_enabled'):
        return {'skipped': 'disabled'}
    if not is_configured(blog):
        return {'skipped': 'credentials_missing'}

    selected = articles[:max_replies]

    # 1. メイン投稿テキストを Gemini に生成させる
    titles_block = '\n'.join(f'- {a.get("title", "")}' for a in selected)
    genre = blog.get('genre') or 'ガジェット'
    main_text = _generate_digest_main(titles_block, genre)
    if not main_text:
        return {'ok': False, 'error': 'main_text_generation_failed'}

    if dry_run:
        previews = [
            {'index': i, 'text': _compose_digest_reply(i, a), 'url': a.get('url', '')}
            for i, a in enumerate(selected)
        ]
        return {
            'dry_run': True,
            'main_text': main_text,
            'replies_preview': previews,
        }

    token = blog['threads_access_token']
    user_id = blog['threads_user_id']
    base = f'https://graph.threads.net/v1.0/{user_id}'

    # 2. メイン投稿
    print(f'[threads-digest] main: {main_text[:80]!r}', flush=True)
    try:
        r1 = requests.post(f'{base}/threads', data={
            'media_type': 'TEXT',
            'text': main_text,
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'main_request: {e}'}
    if r1.status_code >= 400:
        return {'ok': False, 'error': f'main_container HTTP {r1.status_code}: {r1.text[:300]}'}
    cid = (r1.json() or {}).get('id')
    if not cid:
        return {'ok': False, 'error': 'main_no_creation_id'}

    time.sleep(3)

    try:
        r2 = requests.post(f'{base}/threads_publish', data={
            'creation_id': cid, 'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'main_publish_request: {e}'}
    if r2.status_code >= 400:
        return {'ok': False, 'error': f'main_publish HTTP {r2.status_code}: {r2.text[:300]}'}

    parent_id = (r2.json() or {}).get('id')
    print(f'[threads-digest] main OK id={parent_id}', flush=True)

    # 3. リプライ(各60〜120秒間隔、link_attachment付き)
    posted_replies = []
    for i, art in enumerate(selected):
        wait = 60 + i * 30  # 60, 90, 120
        print(f'[threads-digest] sleeping {wait}s before reply[{i}]', flush=True)
        time.sleep(wait)

        reply_text = _compose_digest_reply(i, art)
        url = (art.get('url') or '').strip()
        if not (reply_text and url):
            continue

        try:
            rr = requests.post(f'{base}/threads', data={
                'media_type': 'TEXT',
                'text': reply_text,
                'link_attachment': url,
                'reply_to_id': parent_id,
                'access_token': token,
            }, timeout=20)
        except Exception as e:
            print(f'[threads-digest] reply[{i}] request: {e}', flush=True)
            continue
        if rr.status_code >= 400:
            print(f'[threads-digest] reply[{i}] container HTTP {rr.status_code}: {rr.text[:200]}', flush=True)
            continue
        rcid = (rr.json() or {}).get('id')
        if not rcid:
            continue

        time.sleep(3)

        try:
            rp = requests.post(f'{base}/threads_publish', data={
                'creation_id': rcid, 'access_token': token,
            }, timeout=20)
        except Exception as e:
            print(f'[threads-digest] reply[{i}] publish: {e}', flush=True)
            continue
        if rp.status_code >= 400:
            print(f'[threads-digest] reply[{i}] publish HTTP {rp.status_code}: {rp.text[:200]}', flush=True)
            continue

        tid = (rp.json() or {}).get('id')
        posted_replies.append({'index': i, 'thread_id': tid, 'url': url})
        print(f'[threads-digest] reply[{i}] OK id={tid}', flush=True)

    return {
        'ok': True,
        'main_thread_id': parent_id,
        'replies': posted_replies,
        'count': 1 + len(posted_replies),
    }


_PER_ARTICLE_MAIN_PROMPT = """あなたはThreads(SNS)で「読者が思わずリプ欄のURLをクリックしたくなる投稿」を書くプロです。
以下の記事を告知するThreadsメイン投稿を書いてください。**結論で終わらせず、続きが気になるところで切る(オープンループ)** のが最優先。

【記事タイトル】
{title}

【記事の要約】
{summary}

【★絶対ルール — クリック率を上げる書き方★】

1. **総文字数: 110〜200字**
2. **結論を絶対に書かない** = 「○○がおすすめ」「○○が結論」のような答えで終わらせない。記事の中で一番面白い1点・意外な1点を「これから話す」風に提示してそこで切る。
3. **オープンループ(cliffhanger)で構築**: 状況提示→引っかかり→「答えはこの先」で止める。読者が「で、どうなるの?」と思った瞬間に切る。
4. **末尾は短い誘導1行**で締める。バリエーション例(毎回違うものを使う):
   - 「答えはリプのリンクで」
   - 「全部リプの記事に書いた」
   - 「結論はリプから」
   - 「続きはこのリプで」
   - 「具体的な数字はリプの記事」
   - 「気になる人はリプで」

【書き方のパターン(どれか1つ選んで使う)】

**パターンA: 状況提示 → 意外な発見をチラ見せ → 切る**
例: 「Switch 2が1万円値上げって聞いて『マジかよ』ってなって調べたら、値上げ前にやっておくと1万円損しない裏ワザが実は1つだけあった。コレ知らない人多すぎる。 全部リプの記事に書いた」

**パターンB: 数字や条件を出して「実は◯個だけある」で止める**
例: 「ARグラス7機種真剣に比較したら、これを満たさないやつは全部ハズレって基準が3つあった。値段でも画質でも解像度でもない。 結論はリプから」

**パターンC: 反直感(常識と逆) → 「結論が逆だった」で止める**
例: 「Boston DynamicsのAtlasが冷蔵庫運ぶ動画見て『すげー』で済むかと思いきや、これ実は3年後の引越し業界が消える前兆だった。 答えはリプのリンクで」

**パターンD: 自分の感情を引っ掛ける → 「で、ここからが本題」で止める**
例: 「Anker新型バッテリー、スペックだけ見たら正直微妙。なのに買って3日後に手放せなくなった理由が1つだけある。 全部リプに書いた」

【禁止事項】
- 結論や答えを書く(「○○がおすすめです」「答えは○○です」など)
- 抽象的な感想(「素晴らしい記事です」「興味深いですね」)
- URL・ハッシュタグ・絵文字
- 「ブログを書いた」「読んでください」「フォローしてください」などの押し付け宣伝
- 記事タイトルそのままコピペ

【口調】
ラフで自然な独白調(「マジかよ」「ぶっちゃけ」「正直」「これヤバい」「気になりすぎる」など適度に)。
ただし下品にならず、ガジェット好きが SNS で軽くつぶやく程度の温度感。

出力は本文のみ。前置き・コードフェンス禁止。"""


def post_article_url_reply(blog, title, url, summary='', keywords=None, dry_run=False):
    """1記事ごとの Threads 投稿 (本文メイン + URLリプライ方式)。

    Threads の link_attachment 入りメイン投稿はリーチが下がる傾向がある。
    そこで:
      - メイン: 記事概要 + リプ誘導 (URLなし、画像なし、リンクなし)
      - リプライ: URL + 短文 (link_attachment 付きでカード表示)
    の2段構えにすることで、Threads アルゴリズム的に有利&スパム判定回避。

    1日4本(4時間間隔)程度なら BAN リスクは極小。

    Args:
        blog: Firestore のブログ設定 dict
        title: 記事タイトル
        url: 記事URL
        summary: 記事要約 (Gemini本文生成のヒント)
        keywords: 記事キーワード (現状未使用、将来用)
        dry_run: True なら投稿せずテキストだけ返す

    Returns:
        {'ok': bool, 'main_thread_id'?, 'reply_thread_id'?, 'main_text'?, 'reply_text'?, 'error'?, 'skipped'?}
    """
    if not blog.get('threads_auto_post_enabled'):
        return {'skipped': 'disabled'}
    if not is_configured(blog):
        return {'skipped': 'credentials_missing'}
    if not (title and url):
        return {'skipped': 'no_title_or_url'}

    # 1. メイン投稿のテキストを Gemini で生成
    genre = blog.get('genre') or 'ガジェット'
    main_text = _generate_per_article_main(title, summary, genre)
    if not main_text:
        return {'ok': False, 'error': 'main_text_generation_failed'}

    # 2. リプライ本文 (URLを含む短文)
    reply_text = _compose_url_reply(title, url)

    if dry_run:
        return {
            'dry_run': True,
            'main_text': main_text,
            'reply_text': reply_text,
        }

    token = blog['threads_access_token']
    user_id = blog['threads_user_id']
    base = f'https://graph.threads.net/v1.0/{user_id}'

    # 3. メイン投稿 (link_attachment なし)
    print(f'[threads-art] main: {main_text[:80]!r}', flush=True)
    try:
        r1 = requests.post(f'{base}/threads', data={
            'media_type': 'TEXT',
            'text': main_text,
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'main_request: {e}'}
    if r1.status_code >= 400:
        return {'ok': False, 'error': f'main_container HTTP {r1.status_code}: {r1.text[:300]}'}
    cid = (r1.json() or {}).get('id')
    if not cid:
        return {'ok': False, 'error': 'main_no_creation_id'}

    time.sleep(3)
    try:
        r2 = requests.post(f'{base}/threads_publish', data={
            'creation_id': cid, 'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': False, 'error': f'main_publish_request: {e}'}
    if r2.status_code >= 400:
        return {'ok': False, 'error': f'main_publish HTTP {r2.status_code}: {r2.text[:300]}'}

    main_thread_id = (r2.json() or {}).get('id')
    print(f'[threads-art] main OK id={main_thread_id}', flush=True)

    # 4. 短い間隔を空けてリプライ (URL + link_attachment)
    time.sleep(8)

    try:
        rr = requests.post(f'{base}/threads', data={
            'media_type': 'TEXT',
            'text': reply_text,
            'link_attachment': url,
            'reply_to_id': main_thread_id,
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': True, 'main_thread_id': main_thread_id, 'main_text': main_text,
                'error': f'reply_request: {e}'}
    if rr.status_code >= 400:
        return {'ok': True, 'main_thread_id': main_thread_id, 'main_text': main_text,
                'error': f'reply_container HTTP {rr.status_code}: {rr.text[:200]}'}
    rcid = (rr.json() or {}).get('id')
    if not rcid:
        return {'ok': True, 'main_thread_id': main_thread_id, 'main_text': main_text,
                'error': 'reply_no_creation_id'}

    time.sleep(3)
    try:
        rp = requests.post(f'{base}/threads_publish', data={
            'creation_id': rcid, 'access_token': token,
        }, timeout=20)
    except Exception as e:
        return {'ok': True, 'main_thread_id': main_thread_id, 'main_text': main_text,
                'error': f'reply_publish_request: {e}'}
    if rp.status_code >= 400:
        return {'ok': True, 'main_thread_id': main_thread_id, 'main_text': main_text,
                'error': f'reply_publish HTTP {rp.status_code}: {rp.text[:200]}'}

    reply_thread_id = (rp.json() or {}).get('id')
    print(f'[threads-art] reply OK id={reply_thread_id}', flush=True)

    return {
        'ok': True,
        'main_thread_id': main_thread_id,
        'reply_thread_id': reply_thread_id,
        'main_text': main_text,
        'reply_text': reply_text,
    }


def _generate_per_article_main(title, summary, genre):
    """1記事の Threads メイン投稿本文を Gemini で生成。"""
    from google import genai
    from google.genai import types
    from config import GEMINI_API_KEY, GEMINI_MODEL

    prompt = _PER_ARTICLE_MAIN_PROMPT.format(
        title=(title or '').strip(),
        summary=(summary or '').strip() or '(要約なし)',
        genre=genre,
    )
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.9),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        print(f'[threads-art] main gen failed: {e}', flush=True)
        return ''

    # Sanitize
    text = text.lstrip('"\'').rstrip('"\'').strip()
    # ハッシュタグ・URL混入を除去
    text = re.sub(r'#\S+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) < 40:
        return ''  # 失敗扱い
    if len(text) > 460:
        text = text[:457] + '…'
    return text


_URL_REPLY_INTROS = [
    '答えはここ',
    '結論はこの記事で',
    '全部こっちに書いた',
    '中身はこちら',
    '本題はここから',
    '具体的な数字はこの記事に',
    '裏側はここに書いた',
]


def _compose_url_reply(title, url):
    """URL を貼るリプライ本文を組み立てる。短く、押し付けなく。"""
    import random
    intro = random.choice(_URL_REPLY_INTROS)
    text = f'{intro}\n\n{url}'
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX - 1] + '…'
    return text


def _generate_digest_main(titles_block, genre):
    """ブログ運営者の独り言コメントを Gemini に書かせる。"""
    from google import genai
    from google.genai import types
    from config import GEMINI_API_KEY, GEMINI_MODEL

    prompt = _DAILY_DIGEST_MAIN_PROMPT.format(titles=titles_block, genre=genre)
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.85),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        print(f'[threads-digest] main gen failed: {e}', flush=True)
        # フォールバック: 無難な定型文
        return f'今日は{genre}まわりの動きがいくつかあったので、気になったものを順にメモしておきます。'

    # Sanitize
    text = text.lstrip('"\'').rstrip('"\'').strip()
    # ハッシュタグや絵文字が混入したら除去
    text = re.sub(r'#\S+', '', text).strip()
    if len(text) < 30:
        return f'今日は{genre}関連で気になる動きがいくつかありました。順番に紹介していきます。'
    if len(text) > 450:
        text = text[:447] + '…'
    return text


def _compose_digest_reply(index, art):
    """リプライ本文: つなぎ文言 + 軽い感想 + タイトル + URL"""
    title = _clip(art.get('title') or '', 120)
    url = (art.get('url') or '').strip()

    intros = [
        '気になった1本目はこれ。',
        '2本目はこちら。',
        '最後にこれも。',
        'おまけでもう1本。',
    ]
    intro = intros[min(index, len(intros) - 1)]

    text = (
        f'{intro}\n\n'
        f'{title}\n'
        f'{url}'
    ).strip()
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX - 1] + '…'
    return text


def _compose_chain_text(blog, all_articles, index, art):
    """スレッドチェーン用の本文を組み立てる。"""
    title = _clip(art.get('title') or '', 150)
    url = art.get('url') or ''

    if index == 0:
        # メイン投稿: 「今日の更新まとめ」+ 1記事目 + ハッシュタグ
        n = len(all_articles)
        hashtags = _build_hashtags(blog, art.get('keywords'))
        text = (
            f'今日の更新まとめ({n}本)\n\n'
            f'{title}\n'
            f'{url}\n\n'
            f'続きはリプライで {hashtags}'
        ).strip()
    else:
        # リプライ: つなぎコメント + N本目の記事
        intros = [
            '',  # 0番目 (使わない)
            'こちらもよかったらどうぞ。',
            'もう1本紹介させてください。',
            '最後に、これも気になりました。',
            'おまけでもう1本。',
        ]
        intro = intros[index] if index < len(intros) else 'こちらもどうぞ。'
        text = (
            f'{intro}\n\n'
            f'{title}\n'
            f'{url}'
        ).strip()

    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX - 1] + '…'
    return text


# ---------- helpers ----------

def _build_hashtags(blog, keywords):
    """ジャンル + 記事キーワードから #タグ を作る。最大4個 (Threadsは短い本文なので少なめ)。"""
    tags = []
    seen = set()

    def _add(raw):
        s = (raw or '').strip()
        if not s:
            return
        s = re.sub(r'[^\w぀-ヿ一-鿿]', '', s)
        if not (2 <= len(s) <= 15):
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        tags.append(f'#{s}')

    genre = (blog.get('genre') or '').strip()
    if genre:
        for g in genre.split()[:2]:
            _add(g)
    if keywords:
        for kw in keywords[:2]:
            _add(kw)

    return ' '.join(tags[:4])


def _clip(s, n):
    s = (s or '').strip()
    return s[:n - 1] + '…' if len(s) > n else s
