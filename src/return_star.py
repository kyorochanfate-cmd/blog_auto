"""自分のブログ記事にスターをくれた人に、相手の最新記事へスターを返す。

設計:
- 自分の最近の投稿記事を取得 → 各記事のスター情報を Hatena Star API で取得
- 各 starer のはてなユーザー名から、相手のブログ feed を試行 (.hatenablog.com, .hateblo.jp など)
- 最新記事URLを取得 → スター付与
- 1ユーザーにつき1回だけ返す (Firestore で dedup)
- 1日上限は auto_star と共有 (auto_star_max_per_day)
- Cloud Scheduler が 2時間ごとに叩く想定
"""
import time
import requests
import feedparser

from src.engagement import get_stars
from src.hatena_publisher import _wsse_header
from webapp import blogs as blog_store


_STAR_ADD_API = 'https://s.hatena.ne.jp/star.add.json'

# Hatena blog のサブドメイン候補 (ユーザーIDから推測)
_BLOG_DOMAINS = [
    'hatenablog.com',
    'hatenablog.jp',
    'hateblo.jp',
    'hatenadiary.com',
    'hatenadiary.jp',
    'hatenablog.org',
]


def tick(blog_id, dry_run=False, max_per_tick=5):
    """1tick 分: 最近投稿記事からスターをもらった人を集めて、各人の最新記事に返しスター。

    Returns: dict (skip/error/summary)
    """
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return {'skip': 'blog_not_found'}
    if not blog.get('auto_star_enabled'):
        return {'skip': 'disabled'}

    # 1日上限は auto_star と共有
    cap = int(blog.get('auto_star_max_per_day') or 15)
    today_count = blog_store.count_stars_today(blog_id)
    if today_count >= cap:
        return {'skip': f'day_cap ({today_count}/{cap})'}
    remaining = cap - today_count
    budget = min(max_per_tick, remaining)
    if budget <= 0:
        return {'skip': 'no_budget'}

    articles = blog_store.list_published_articles(blog_id, limit=30)
    if not articles:
        return {'skip': 'no_articles'}

    # 既に返したユーザー
    starred_users = blog_store.get_returned_star_users(blog_id)

    candidates = []  # [(user, target_url), ...]
    my_id = (blog.get('hatena_id') or '').lower()

    for article in articles:
        article_url = article.get('url')
        if not article_url:
            continue
        stars_data = get_stars(article_url)
        if not stars_data:
            continue
        for entry in (stars_data.get('entries') or []):
            star_blocks = list(entry.get('stars') or [])
            for cs in (entry.get('colored_stars') or []):
                star_blocks.extend(cs.get('stars') or [])
            for s in star_blocks:
                user = (s.get('name') or '').strip()
                if not user or user.lower() == my_id:
                    continue
                if user in starred_users:
                    continue
                if any(c[0] == user for c in candidates):
                    continue  # 同じユーザーは1回だけキュー
                candidates.append((user, None))

        if len(candidates) >= budget * 3:
            break  # 早めに切り上げ (Cloud Run timeout回避)

    if not candidates:
        return {'skip': 'no_new_starers'}

    print(f'[return-star] candidates: {len(candidates)}, budget: {budget}', flush=True)

    results = []
    for user, _ in candidates:
        if len(results) >= budget:
            break
        target = _find_latest_entry(user)
        if not target:
            print(f'[return-star] no feed for @{user}, skip', flush=True)
            continue

        if dry_run:
            results.append({'would_star': target, 'user': user})
            continue

        r = _add_star(blog, target)
        if r.get('ok'):
            try:
                blog_store.record_returned_star(blog_id, user, target)
                blog_store.record_starred_url(blog_id, target, f'@{user} の返信スター')
            except Exception as e:
                print(f'[return-star] record failed: {e}', flush=True)
            print(f'[return-star] OK: @{user} -> {target}', flush=True)
            results.append({'starred': target, 'user': user})
        else:
            print(f'[return-star] FAIL @{user}: {r.get("error")}', flush=True)
            results.append({'error': r.get('error'), 'user': user})

        time.sleep(2)  # ペース調整

    return {'returned': len([r for r in results if r.get('starred')]), 'details': results}


def _find_latest_entry(hatena_username):
    """ユーザーの最新ブログ記事URLを RSS 経由で取得。"""
    if not hatena_username or '/' in hatena_username:
        return None
    for domain in _BLOG_DOMAINS:
        url = f'https://{hatena_username}.{domain}/feed'
        try:
            r = requests.get(url, timeout=8, headers={
                'User-Agent': 'gadget-blog-bot/1.0 (return-star)',
            })
        except Exception:
            continue
        if r.status_code != 200 or not r.content:
            continue
        try:
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        if not feed.entries:
            continue
        link = feed.entries[0].get('link')
        if link and link.startswith('http'):
            return link
    return None


def _add_star(blog, target_url):
    if not (blog.get('hatena_id') and blog.get('hatena_api_key')):
        return {'ok': False, 'error': 'hatena_credentials_missing'}
    try:
        r = requests.post(
            _STAR_ADD_API,
            data={'uri': target_url},
            headers={
                'X-WSSE': _wsse_header(blog['hatena_id'], blog['hatena_api_key']),
                'User-Agent': 'gadget-blog-bot/1.0',
            },
            timeout=15,
        )
    except Exception as e:
        return {'ok': False, 'error': f'request_exception: {e}'}

    if r.status_code < 400:
        return {'ok': True}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}'}
