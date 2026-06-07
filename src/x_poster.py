"""X (Twitter) 自動投稿。

記事公開直後に呼び出され、X API v2 (POST /2/tweets) で告知ツイートを投稿する。

認証: OAuth 1.0a User Context (Consumer Key/Secret + Access Token/Secret の4点)
ブログごとに認証情報を保存するので、複数アカウント使い分けOK。

レート制限 (X Free tier):
- 1,500ツイート/月
- 50ツイート/24時間
これらを超えた場合は X 側が 429 を返す。失敗してもログだけ残して処理は継続。
"""
import re
import tweepy


# プレースホルダ:
#  {title} {url} {summary} {hashtags}
DEFAULT_TEMPLATE = '{title}\n\n{url}\n\n{hashtags}'

# X のツイート上限 = 280文字。URLは X 側で t.co に短縮されて23文字扱い。
# 余裕を持って 270 でcap。
_TWEET_MAX = 270
_URL_TCO_LEN = 23


def is_configured(blog):
    return all(blog.get(k) for k in (
        'x_api_key', 'x_api_secret', 'x_access_token', 'x_access_token_secret'
    ))


def post_article(blog, title, url, summary='', keywords=None):
    """記事を X に告知ツイート。

    Returns:
        {'ok': bool, 'tweet_id'?: str, 'text'?: str, 'error'?: str, 'skipped'?: str}
    例外を投げない (記事公開のフローをブロックしないため)。
    """
    if not blog.get('x_auto_post_enabled'):
        return {'skipped': 'disabled'}
    if not is_configured(blog):
        return {'skipped': 'credentials_missing'}
    if not (title and url):
        return {'skipped': 'no_title_or_url'}

    template = (blog.get('x_template') or '').strip() or DEFAULT_TEMPLATE
    hashtags = _build_hashtags(blog, keywords)
    tweet_text = (
        template
        .replace('{title}', _clip(title, 80))
        .replace('{url}', url)
        .replace('{summary}', _clip(summary or '', 100))
        .replace('{hashtags}', hashtags)
    )
    tweet_text = _fit_into_limit(tweet_text, url)

    try:
        client = tweepy.Client(
            consumer_key=blog['x_api_key'],
            consumer_secret=blog['x_api_secret'],
            access_token=blog['x_access_token'],
            access_token_secret=blog['x_access_token_secret'],
        )
        resp = client.create_tweet(text=tweet_text)
        tweet_id = (resp.data or {}).get('id') if hasattr(resp, 'data') else None
        print(f'[x] OK id={tweet_id} text={tweet_text[:60]!r}...', flush=True)
        return {'ok': True, 'tweet_id': tweet_id, 'text': tweet_text}
    except tweepy.TooManyRequests as e:
        print(f'[x] RATE_LIMIT: {e}', flush=True)
        return {'ok': False, 'error': 'rate_limit', 'detail': str(e)}
    except tweepy.Forbidden as e:
        # よくある原因: 権限が Read のまま、または重複ツイート
        print(f'[x] FORBIDDEN: {e}', flush=True)
        return {'ok': False, 'error': 'forbidden', 'detail': str(e)}
    except Exception as e:
        print(f'[x] FAIL: {type(e).__name__}: {e}', flush=True)
        return {'ok': False, 'error': type(e).__name__, 'detail': str(e)}


# ---------- helpers ----------

def _build_hashtags(blog, keywords):
    """ジャンル + 記事キーワードから #タグ を作る。最大5個。"""
    tags = []
    seen = set()

    def _add(raw):
        s = (raw or '').strip()
        if not s:
            return
        # ハッシュタグに使える文字に絞る (英数字・日本語)
        s = re.sub(r'[^\w぀-ヿ一-鿿]', '', s)
        if not (2 <= len(s) <= 15):
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        tags.append(f'#{s}')

    # ジャンルから2個まで
    genre = (blog.get('genre') or '').strip()
    if genre:
        for g in genre.split()[:2]:
            _add(g)

    # 記事キーワードから3個まで
    if keywords:
        added = 0
        for kw in keywords:
            if added >= 3:
                break
            _add(kw)
            if len(tags) > 0:
                added += 1

    return ' '.join(tags[:5])


def _clip(s, n):
    s = (s or '').strip()
    return s[:n - 1] + '…' if len(s) > n else s


def _fit_into_limit(text, url):
    """X 換算 (URL=23文字扱い) で270以内に収める。超えたら title を縮める。"""
    # X換算長を計算
    counted = _x_count(text, url)
    if counted <= _TWEET_MAX:
        return text

    # 超えた場合: 1行目 (タイトル想定) を削る
    lines = text.split('\n')
    if not lines:
        return text[:_TWEET_MAX]
    over = counted - _TWEET_MAX + 3  # 「…」分の余裕
    head = lines[0]
    if len(head) > over + 5:
        lines[0] = head[:len(head) - over] + '…'
        return '\n'.join(lines)
    # それでも収まらないなら強制トランケート
    rebuilt = '\n'.join(lines)
    while _x_count(rebuilt, url) > _TWEET_MAX and len(rebuilt) > 50:
        rebuilt = rebuilt[:-1]
    return rebuilt


def _x_count(text, url):
    """X換算文字数 (URLは23文字扱い)。"""
    if not url or url not in text:
        return len(text)
    return len(text) - len(url) + _URL_TCO_LEN
