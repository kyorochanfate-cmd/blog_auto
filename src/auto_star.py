"""はてなスターを他ブログに自動で付ける。

設計 (BAN リスク低めの控えめ運用):
- Cloud Scheduler が 30分おきに tick を叩く
- 稼働時間: JSTの所定時間帯のみ (デフォルト 9-22 時)
- 確率トリガ: 各 tick で probability の確率でのみ発火 (デフォルト 25%)
- 1日上限: max_per_day (デフォルト 5)
- ジッタ: 発火時に 5〜60 秒のランダム待機 (cron時刻にぴったり張り付かない)
- 重複防止: 一度スター付けた URL は Firestore で記録、二度打ちしない
- ターゲット選定: はてブIT 人気・新着から、自ブログのキーワードで重み付け

平均: 6-7 スター/日 (28 ticks × 25% = 7、ただし1日上限で頭打ち5)
"""
import random
import time
from datetime import datetime, timezone, timedelta
import requests

from src.discover import fetch_engagement_targets, get_user_keywords_from_blog
from src.hatena_publisher import _wsse_header
from webapp import blogs as blog_store


_JST = timezone(timedelta(hours=9))

_STAR_ADD_API = 'https://s.hatena.ne.jp/star.add.json'


def tick(blog_id, dry_run=False):
    """1tick 分の処理。確率と1日上限で間引きながらスターを1個付ける。

    Returns:
        skipped: {'skip': '<reason>'}
        success: {'starred': '<url>', 'title': '<title>'}
        failure: {'error': '<message>'}
        dry-run when would-star: {'would_star': '<url>', 'title': '<title>', 'jitter': N}
    """
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return {'skip': 'blog_not_found'}
    if not blog.get('auto_star_enabled'):
        return {'skip': 'disabled'}

    # 稼働時間チェック (JST)
    now_jst = datetime.now(_JST)
    start, end = _parse_hours(blog.get('auto_star_hours_jst') or '9-22')
    if not (start <= now_jst.hour <= end):
        return {'skip': f'outside_hours (now {now_jst.hour}:00 JST, window {start}-{end})'}

    # 1日上限
    cap = int(blog.get('auto_star_max_per_day') or 5)
    today_count = blog_store.count_stars_today(blog_id)
    if today_count >= cap:
        return {'skip': f'day_cap_reached ({today_count}/{cap})'}

    # 確率ロール
    p = _coerce_float(blog.get('auto_star_probability'), 0.25)
    if random.random() > p:
        return {'skip': f'dice_miss (p={p:.2f})'}

    # ジッタ (cron時刻にぴったり張り付かない用)
    jitter = random.randint(5, 60)
    if not dry_run:
        print(f'[star] dice_hit, sleeping {jitter}s before action', flush=True)
        time.sleep(jitter)

    # 候補取得
    try:
        keywords = get_user_keywords_from_blog(blog_id)
    except Exception as e:
        print(f'[star] keywords lookup failed: {e}', flush=True)
        keywords = []

    try:
        targets = fetch_engagement_targets(per_feed=30, my_blog_keywords=keywords)
    except Exception as e:
        print(f'[star] target fetch failed: {e}', flush=True)
        return {'error': f'target_fetch: {e}'}

    # 既スター除外
    try:
        starred = blog_store.get_starred_urls(blog_id)
    except Exception as e:
        print(f'[star] starred lookup failed (treating as empty): {e}', flush=True)
        starred = set()
    targets = [t for t in targets if t.get('url') not in starred]

    if not targets:
        return {'skip': 'no_new_candidates'}

    # スコア重み付きで1件ピック (上位20件から)
    pool = targets[:20]
    weights = [(t.get('score') or 0) + 1 for t in pool]
    target = random.choices(pool, weights=weights, k=1)[0]

    if dry_run:
        return {
            'would_star': target.get('url'),
            'title': target.get('title'),
            'score': target.get('score'),
            'jitter_seconds': jitter,
        }

    # 実際にスター付与
    result = _add_star(blog, target.get('url'))
    if result.get('ok'):
        try:
            blog_store.record_starred_url(blog_id, target.get('url'), target.get('title', ''))
        except Exception as e:
            print(f'[star] record failed (continuing): {e}', flush=True)
        print(f'[star] OK: {target.get("url")} ("{target.get("title", "")[:40]}")', flush=True)
        return {
            'starred': target.get('url'),
            'title': target.get('title'),
            'score': target.get('score'),
        }

    print(f'[star] FAIL: {result.get("error")}', flush=True)
    return {'error': result.get('error')}


# ---------- helpers ----------

def _parse_hours(spec):
    """'9-22' → (9, 22)。不正値は (9, 22) にフォールバック。"""
    try:
        a, b = spec.split('-')
        a, b = int(a), int(b)
        if 0 <= a <= 23 and 0 <= b <= 23 and a <= b:
            return a, b
    except Exception:
        pass
    return 9, 22


def _coerce_float(v, default):
    try:
        return float(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _add_star(blog, target_url):
    """Hatena Star API: POST /star.add.json (WSSE 認証)。

    既存の hatena_id + hatena_api_key を流用 (AtomPub と同じ資格情報)。
    """
    if not target_url:
        return {'ok': False, 'error': 'no_url'}
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
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:300]}'}
