"""自分のブログ記事に付いたHatena Star / Bookmarkコメントを集約。

公開API:
- https://s.hatena.ne.jp/entry.json?uri=URL → スター情報
- https://b.hatena.ne.jp/entry/jsonlite/?url=URL → ブックマーク・コメント

両方ともpublic、認証不要。
"""
import requests


_TIMEOUT = 10


def get_stars(article_url):
    """Hatena Star API: 記事URLについたスターを返す。

    戻り値の構造例:
    {
        'entries': [
            {'uri': '...', 'stars': [{'name': 'taro'}, {'count': 5, 'name': 'jiro'}, ...],
             'colored_stars': [{'color': 'yellow', 'stars': [...]}, ...]}
        ]
    }
    """
    if not article_url:
        return None
    try:
        r = requests.get(
            'https://s.hatena.ne.jp/entry.json',
            params={'uri': article_url},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data
    except Exception as e:
        print(f'[engagement] stars failed: {e}', flush=True)
        return None


def get_bookmarks(article_url):
    """Hatena Bookmark JSON Lite: 記事URLについたブクマ・コメントを返す。

    戻り値の構造例:
    {
        'count': N,
        'bookmarks': [{'user': 'taro', 'comment': '面白い', 'timestamp': '...', 'tags': []}, ...],
        'eid': '...',
        'title': '...',
        'url': '...',
    }
    """
    if not article_url:
        return None
    try:
        r = requests.get(
            'https://b.hatena.ne.jp/entry/jsonlite/',
            params={'url': article_url},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f'[engagement] bookmarks failed: {e}', flush=True)
        return None


def aggregate_for_article(article_url):
    """1記事のスター総数 + ブックマーク数 + コメント一覧を返す。"""
    star_total = 0
    star_users = []
    stars_data = get_stars(article_url)
    if stars_data:
        for entry in (stars_data.get('entries') or []):
            for s in (entry.get('stars') or []):
                # 通常 {'name': 'user'} または {'name': 'user', 'count': N}
                cnt = s.get('count', 1)
                star_total += cnt
                star_users.append(s.get('name', ''))
            for cs in (entry.get('colored_stars') or []):
                for s in (cs.get('stars') or []):
                    cnt = s.get('count', 1)
                    star_total += cnt
                    star_users.append(s.get('name', ''))

    bookmark_count = 0
    comments = []
    bm_data = get_bookmarks(article_url)
    if bm_data:
        bookmark_count = bm_data.get('count', 0)
        for bm in (bm_data.get('bookmarks') or []):
            comment_text = (bm.get('comment') or '').strip()
            if comment_text:
                comments.append({
                    'user': bm.get('user', ''),
                    'comment': comment_text,
                    'timestamp': bm.get('timestamp', ''),
                    'tags': bm.get('tags', []),
                })

    return {
        'star_total': star_total,
        'star_users': list(dict.fromkeys(star_users))[:20],  # 重複除去・最大20
        'bookmark_count': bookmark_count,
        'comments': comments,
    }


def draft_reply(article_title, comment_text, tone_hint=''):
    """コメントへのお礼/返信文をAIに下書きさせる。"""
    from google import genai
    from google.genai import types
    from config import GEMINI_API_KEY, GEMINI_MODEL

    prompt = f"""以下のはてなブックマークコメントに対する、ブログ運営者からの返信を1つ書いてください。

【記事タイトル】 {article_title}
【もらったコメント】 {comment_text}
{f'【口調ヒント】{tone_hint}' if tone_hint else ''}

ルール:
- 80〜150文字
- お礼から入る
- コメントの内容を1つ拾って具体的に応答
- 慇懃すぎない・自然なブログ運営者の口調
- 絵文字なし
- 出力は返信文のみ。前置き不要。
"""
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.7),
        )
        return (resp.text or '').strip()
    except Exception as e:
        print(f'[engagement] reply draft failed: {e}', flush=True)
        return ''
