import json
import os
import traceback
from functools import wraps

from flask import Flask, request, redirect, url_for, session, jsonify
import markdown as md_lib

import config
from src.news_collector import collect_recent_items
from src.topic_selector import select_top_topics, generate_policy_topics
from src.researcher import fetch_article_texts
from src.article_generator import generate_article, revise_article, generate_comparison_article, generate_ranking_article
from src.image_finder import find_images, resolve_official_image
from src.hatena_publisher import publish, upload_photo
from src.structured_data import build_jsonld, append_jsonld
from src.quality_scorer import score_article, build_improvement_instructions
from src.longtail import suggest_longtail
from src.indexnow import submit_url as indexnow_submit
from src.image_rehost import rehost_external_images
from src.trends import fetch_trends
from src.theme_gap import suggest_missing_themes
from src.discover import fetch_engagement_targets, get_user_keywords_from_blog
from src.engagement import aggregate_for_article, draft_reply
from src.article_presets import PRESETS as STYLE_PRESETS, apply_preset as apply_style_preset
from src import auto_runner
from src import google_indexing
from src import x_poster
from src import threads_poster
from src.categorizer import suggest_categories
from webapp import blogs as blog_store


app = Flask(__name__, template_folder='templates', static_folder='static')
app.jinja_env.globals['STYLE_PRESETS'] = STYLE_PRESETS
app.secret_key = config.FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)


@app.errorhandler(500)
def handle_500(e):
    tb = traceback.format_exc()
    app.logger.error(f'500 error:\n{tb}')
    return f'<pre style="white-space:pre-wrap;padding:1em">エラー詳細:\n{tb}</pre>', 500


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('authed'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def _verify_auto_run_token():
    """Cloud Scheduler からのアクセスを検証 (共有シークレット)。

    Authorization ヘッダの "Bearer <token>" または ?token=<token> クエリパラメータで受け取る。
    config.AUTO_RUN_TOKEN が設定されていない場合は無効化 (常に拒否)。
    """
    expected = getattr(config, 'AUTO_RUN_TOKEN', '')
    if not expected:
        return False
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        provided = auth[7:].strip()
    else:
        provided = request.args.get('token', '').strip()
    return provided == expected


# ---------- UI ルート削除済み (Hatena 管理画面で直接運用) ----------

@app.route('/auto-run/<blog_id>', methods=['POST', 'GET'])
def auto_run_blog(blog_id):
    """指定ブログを自動運用 (Cloud Scheduler から1ブログ1ジョブで呼ぶ前提)。

    認証: Authorization: Bearer <AUTO_RUN_TOKEN> または ?token=<...>
    Cloud Runのタイムアウトを超えないよう1ブログずつのエンドポイントとした。
    """
    # ログイン済みなら手動キックも許可 (テスト用)
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401

    dry = request.args.get('dry') == '1'
    try:
        result = auto_runner.run_for_blog(blog_id, dry_run=dry)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/auto-star-tick/<blog_id>', methods=['POST', 'GET'])
def auto_star_tick_route(blog_id):
    """30分おきに Cloud Scheduler から呼ばれる auto_star tick。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    from src import auto_star
    dry = request.args.get('dry') == '1'
    try:
        result = auto_star.tick(blog_id, dry_run=dry)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/threads-daily-digest/<blog_id>', methods=['POST', 'GET'])
def threads_daily_digest_route(blog_id):
    """1日1回 Cloud Scheduler から呼ばれる Threads ダイジェスト投稿。
    過去24時間に公開した記事から最大3本を選び、メイン1+リプライ1-3でまとめ投稿。
    スパム判定回避のため日次1回のみ。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    from src import threads_poster
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    dry = request.args.get('dry') == '1'
    try:
        articles = blog_store.get_recent_published_articles(blog_id, hours=24, limit=3)
        if not articles:
            return jsonify({'skipped': 'no_articles_in_last_24h'})
        result = threads_poster.post_daily_digest(blog, articles, max_replies=3, dry_run=dry)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/threads-token-refresh', methods=['POST', 'GET'])
def threads_token_refresh_route():
    """日次 cron から呼ばれて、Threads長期トークンの自動延長を試行。
    残り14日以下のブログをまとめてチェック → 延長 → 失敗時 Gmail 通知。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    from src import threads_token_refresh
    try:
        result = threads_token_refresh.check_and_refresh_all()
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/return-star-tick/<blog_id>', methods=['POST', 'GET'])
def return_star_tick_route(blog_id):
    """数時間おきに Cloud Scheduler から呼ばれる return-star tick。
    自分の記事にスターをくれた人の最新記事へスターを返す。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    from src import return_star
    dry = request.args.get('dry') == '1'
    try:
        result = return_star.tick(blog_id, dry_run=dry)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/prune-articles/<blog_id>', methods=['POST', 'GET'])
def prune_articles_route(blog_id):
    """低価値記事(3日以上前 + クリック≤1 or 画像なし)を判定し削除。
    クエリ:
      ?dry=1         …プレビュー(削除しない)
      ?days_age=3    …何日以上経過の記事を対象に
      ?days_gsc=28   …GSC集計日数
      ?limit=50      …削除/プレビュー上限件数
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    dry = request.args.get('dry') == '1'
    days_age = int(request.args.get('days_age', '3'))
    days_gsc = int(request.args.get('days_gsc', '28'))
    limit_raw = request.args.get('limit')
    limit = int(limit_raw) if limit_raw else None

    from src import article_pruner
    try:
        result = article_pruner.prune_articles(
            blog, days_age=days_age, days_gsc=days_gsc, dry_run=dry, limit=limit,
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/queue-hubs/<blog_id>', methods=['POST', 'GET'])
def queue_hubs_route(blog_id):
    """全クラスタのハブ記事を生成し、Firestore キューに登録 (公開はしない)。
    クエリ: ?min_cluster=4
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404
    from src import hub_generator
    min_cluster = int(request.args.get('min_cluster', '4'))
    try:
        result = hub_generator.queue_all_hubs(blog, min_cluster=min_cluster)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/queued-hubs/<blog_id>', methods=['GET'])
def list_queued_hubs_route(blog_id):
    """キュー中のハブ記事一覧。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    from src import hub_generator
    items = hub_generator.get_queued_hubs(blog_id)
    return jsonify({'count': len(items), 'items': items})


@app.route('/admin/publish-next-hub/<blog_id>', methods=['POST', 'GET'])
def publish_next_hub_route(blog_id):
    """キューから1件公開 (Cloud Scheduler から日次で叩く想定)。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404
    from src import hub_generator
    try:
        result = hub_generator.publish_next_hub(blog)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/build-hubs/<blog_id>', methods=['POST', 'GET'])
def build_hubs_route(blog_id):
    """既存記事をテーマ別クラスタリングしてハブ(ピラー)記事を生成。

    クエリ:
      ?dry=1            プレビュー(投稿せず本文を返す)
      ?min_cluster=4   1クラスタの最低記事数
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    dry = request.args.get('dry') == '1'
    min_cluster = int(request.args.get('min_cluster', '4'))

    from src import hub_generator
    try:
        result = hub_generator.run_hub_pipeline(blog, min_cluster=min_cluster, dry_run=dry)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/rewrite-from-gsc/<blog_id>', methods=['POST', 'GET'])
def rewrite_from_gsc_route(blog_id):
    """Search Console データに基づいて記事タイトル+導入をリライト。

    クエリパラメータ:
      ?dry=1   …プレビュー(更新せず)
      ?n=3     …トップ何件処理するか (default 3)
      ?days=28 …何日分のSCデータを使うか (default 28)
      ?min_impr=30 …最低インプ数 (default 30)
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    dry = request.args.get('dry') == '1'
    n = int(request.args.get('n', '3'))
    days = int(request.args.get('days', '28'))
    min_impr = int(request.args.get('min_impr', '30'))

    from src import article_rewriter
    try:
        result = article_rewriter.run_rewrite_pipeline(
            blog, days=days, top_n=n, min_impressions=min_impr, dry_run=dry,
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/append-banner-latest/<blog_id>', methods=['POST', 'GET'])
def append_banner_latest_route(blog_id):
    """最新公開記事にグループバナーを追加 (pending publish 経由のバナー漏れ救済)。"""
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404
    from src import affiliate_upgrade
    try:
        result = affiliate_upgrade.append_banner_to_latest(blog)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/scrub-dead-links/<blog_id>', methods=['POST', 'GET'])
def scrub_dead_links_route(blog_id):
    """全公開記事の本文中の Markdown 内部リンクを HEAD チェックし、404 のものを除去。
    クエリ: ?dry=1 でプレビュー、?limit=N で対象記事数制限
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    dry = request.args.get('dry') == '1'
    limit_raw = request.args.get('limit')
    article_limit = int(limit_raw) if limit_raw else None

    import requests as _req
    import re as _re
    from src import affiliate_upgrade

    # 同じドメインの内部リンク用パターン
    domain = blog.get('hatena_blog_domain', '')
    internal_link_re = _re.compile(r'\[([^\]]+)\]\((https?://' + _re.escape(domain) + r'/entry/[^)]+)\)')

    # 全公開記事を取得
    page_url = None
    all_entries = []
    for _ in range(50):
        entries, next_link = affiliate_upgrade._list_entries(blog, page_url)
        all_entries.extend([e for e in entries if e['draft'] != 'yes'])
        if not next_link:
            break
        page_url = next_link

    if article_limit:
        all_entries = all_entries[:article_limit]

    # URL → alive/dead キャッシュ (重複チェック回避)
    url_alive_cache = {}
    def check_alive(url):
        if url in url_alive_cache:
            return url_alive_cache[url]
        try:
            r = _req.head(url, timeout=8, allow_redirects=True)
            ok = r.status_code < 400
        except Exception:
            ok = False
        url_alive_cache[url] = ok
        return ok

    summary = {'total_articles': len(all_entries), 'updated': 0, 'links_removed': 0, 'examples': []}
    for e in all_entries:
        try:
            full = affiliate_upgrade.fetch_entry(blog, e['edit_url'])
            body = full.get('content') or ''
        except Exception:
            continue
        matches = list(internal_link_re.finditer(body))
        if not matches:
            continue
        # 死リンク特定
        dead_links_in_article = []
        for m in matches:
            url = m.group(2)
            if not check_alive(url):
                dead_links_in_article.append((m.group(0), m.group(1), url))
        if not dead_links_in_article:
            continue

        new_body = body
        for orig_md, link_text, dead_url in dead_links_in_article:
            # 「[text](url)」 → 「text」 (リンクテキストだけ残す)
            new_body = new_body.replace(orig_md, link_text)

        # 箇条書きで「- text」だけが残るパターンの行を丸ごと削除 (リンクだったが切れた行)
        new_body = _re.sub(r'^\s*-\s+([^\n]+)\n', lambda m_: '' if any(d[1] == m_.group(1).strip() for d in dead_links_in_article) else m_.group(0), new_body, flags=_re.MULTILINE)

        if new_body == body:
            continue

        summary['links_removed'] += len(dead_links_in_article)
        summary['examples'].append({
            'title': full.get('title', '')[:60],
            'url': e.get('alternate_url', ''),
            'dead_count': len(dead_links_in_article),
        })

        if not dry:
            try:
                affiliate_upgrade.update_entry(blog, e['edit_url'], full.get('title', ''), new_body, full.get('categories', []))
                summary['updated'] += 1
            except Exception as ex:
                summary['examples'][-1]['error'] = str(ex)[:120]

    summary['dry_run'] = dry
    return jsonify(summary)


@app.route('/admin/cleanup-dead-records/<blog_id>', methods=['POST', 'GET'])
def cleanup_dead_records_route(blog_id):
    """published_articles の中で URL が 404/410 のレコードを全削除。
    削除済み記事への内部リンクで死リンクが生まれるのを防ぐ。
    クエリ: ?dry=1 でプレビュー
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401

    dry = request.args.get('dry') == '1'
    import requests as _req

    articles = blog_store.list_published_articles(blog_id, limit=5000)
    dead = []
    alive = 0
    for a in articles:
        url = a.get('url', '')
        if not url:
            continue
        try:
            r = _req.head(url, timeout=10, allow_redirects=True)
            status = r.status_code
        except Exception:
            status = 0
        if status in (404, 410) or status == 0:
            dead.append({'title': a.get('title', '')[:60], 'url': url, 'status': status})
        else:
            alive += 1

    deleted = 0
    if not dry:
        for d in dead:
            try:
                blog_store.delete_published_article_record(d['url'])
                deleted += 1
            except Exception as e:
                d['delete_error'] = str(e)[:100]

    return jsonify({
        'total_scanned': len(articles),
        'alive': alive,
        'dead_count': len(dead),
        'deleted': deleted,
        'dead_preview': dead[:20],
        'dry_run': dry,
    })


@app.route('/admin/set-blog-flag/<blog_id>', methods=['POST', 'GET'])
def set_blog_flag_route(blog_id):
    """ブログの単一フィールドだけを直接 Firestore に更新する (他フィールド非破壊)。
    クエリ: ?key=use_claude_writing&value=true (bool) or ?key=niche_focus&value=AR/VR (str)
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    key = (request.args.get('key') or '').strip()
    value_raw = (request.args.get('value') or '').strip()

    allowed_bool_keys = {
        'use_claude_writing', 'use_charts', 'use_wiki_images',
        'auto_publish_enabled', 'x_auto_post_enabled', 'threads_auto_post_enabled',
        'auto_star_enabled',
    }
    allowed_str_keys = {
        'genre', 'niche_focus', 'tone_prompt', 'article_policy', 'topic_policy',
        'hatena_category',
    }
    if key not in allowed_bool_keys and key not in allowed_str_keys:
        return jsonify({'error': f'key not allowed', 'allowed': list(allowed_bool_keys | allowed_str_keys)}), 400

    if key in allowed_bool_keys:
        vl = value_raw.lower()
        if vl in ('true', '1', 'yes'):
            value = True
        elif vl in ('false', '0', 'no'):
            value = False
        else:
            return jsonify({'error': 'bool value must be true/false'}), 400
    else:
        value = value_raw  # string as-is

    from google.cloud import firestore
    client = firestore.Client()
    client.collection('blogs').document(blog_id).update({key: value})
    return jsonify({'ok': True, 'blog_id': blog_id, 'key': key, 'value': value})


@app.route('/admin/next-pending-write', methods=['GET', 'POST'])
def next_pending_write_route():
    """Claude Code scheduled agent が叩く: 最古の pending を取得 + status='writing' に更新。
    クエリ: ?blog_id=xxx (任意)
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog_id = request.args.get('blog_id', '').strip() or None
    from src import article_queue
    try:
        doc = article_queue.claim_next_pending(blog_id=blog_id)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    if not doc:
        return jsonify({'pending': False})
    # context 内の datetime はそのままだとJSON化できないのでstr化
    safe_doc = {
        'doc_id': doc.get('doc_id'),
        'blog_id': doc.get('blog_id'),
        'topic_name': doc.get('topic_name'),
        'topic_summary': doc.get('topic_summary'),
        'prompt': doc.get('prompt'),
        'status': doc.get('status'),
    }
    return jsonify({'pending': True, **safe_doc})


@app.route('/admin/submit-written-body/<doc_id>', methods=['POST'])
def submit_written_body_route(doc_id):
    """Claude Code agent が書き終えた本文を受け取り、後段処理→公開する。
    POST body (JSON): {"title": "...", "body": "..."}
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    if not (title and body):
        return jsonify({'error': 'title and body required'}), 400
    try:
        result = auto_runner.finalize_from_queue(doc_id, title, body)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/claude-fallback', methods=['POST', 'GET'])
def claude_fallback_route():
    """30分以上 pending のままの記事を Gemini で書いて自動公開する保険cron。
    クエリ: ?stale=30 ?limit=3
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    stale = int(request.args.get('stale', '30'))
    limit = int(request.args.get('limit', '3'))
    try:
        result = auto_runner.run_claude_fallback(stale_minutes=stale, limit=limit)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/pending-writes-status', methods=['GET'])
def pending_writes_status_route():
    """キューの状況確認 (運用監視用)。
    クエリ: ?blog_id=xxx (任意)
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog_id = request.args.get('blog_id', '').strip() or None
    from src import article_queue
    return jsonify(article_queue.count_by_status(blog_id=blog_id))


@app.route('/admin/threads-post-latest/<blog_id>', methods=['POST', 'GET'])
def threads_post_latest_route(blog_id):
    """ブログの最新公開記事を Threads にメイン+URLリプライ方式で投稿する。
    手動公開した記事の告知用エンドポイント。
    認証: セッション or Bearer AUTO_RUN_TOKEN
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    from src import affiliate_upgrade, threads_poster
    try:
        entry = affiliate_upgrade.get_latest_published_entry(blog)
        if not entry:
            return jsonify({'error': 'no published entry found'}), 404
        result = threads_poster.post_article_url_reply(
            blog,
            entry['title'],
            entry['url'],
            summary=entry['summary'],
        )
        return jsonify({'entry': entry, 'threads': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/admin/rewrite-article/<blog_id>', methods=['POST', 'GET'])
def rewrite_article_route(blog_id):
    """既存記事を完全リライト (URL維持・現在の品質基準で書き直し)。
    クエリ:
      ?title=remarkable   …タイトル部分一致で記事を特定
      ?dry=1              …プレビュー(更新せず)
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401
    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    title_q = (request.args.get('title') or '').strip()
    if not title_q:
        return jsonify({'error': 'query param "title" is required'}), 400

    dry = request.args.get('dry') == '1'
    # POST body の extra_source は追加コンテキスト (公式スペック貼り付け等)
    extra = ''
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        extra = (data.get('extra_source') or '').strip()
    from src import affiliate_upgrade
    try:
        result = affiliate_upgrade.rewrite_one_article_completely(
            blog, title_q, dry_run=dry, extra_source_text=extra,
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/admin/upgrade-affiliate/<blog_id>', methods=['POST', 'GET'])
def upgrade_affiliate_route(blog_id):
    """既存記事の旧Amazon/楽天テキストリンクを最新の商品カードに置換する。

    使い方: /admin/upgrade-affiliate/<blog_id>?title=Jackery
    認証: セッションログイン or Authorization: Bearer <AUTO_RUN_TOKEN>
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401

    blog = blog_store.get_blog(blog_id)
    if not blog:
        return jsonify({'error': 'blog not found'}), 404

    title_q = (request.args.get('title') or '').strip()
    if not title_q:
        return jsonify({'error': 'query param "title" is required'}), 400

    from src import affiliate_upgrade
    try:
        result = affiliate_upgrade.upgrade_one_article(blog, title_q)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return jsonify(result)


@app.route('/auto-run', methods=['POST', 'GET'])
def auto_run_all():
    """全ての auto_publish_enabled=True ブログを順に処理。

    注意: ブログ数 × 記事数 × 30〜60秒 が Cloud Run timeout (最大3600秒) を超えないこと。
    超える場合はブログごとに別 Cloud Scheduler ジョブを立てて /auto-run/<blog_id> を呼ぶ。
    """
    if not session.get('authed') and not _verify_auto_run_token():
        return jsonify({'error': 'unauthorized'}), 401

    dry = request.args.get('dry') == '1'
    summaries = []
    for blog in blog_store.list_blogs():
        if not blog.get('auto_publish_enabled'):
            continue
        try:
            summaries.append(auto_runner.run_for_blog(blog['id'], dry_run=dry))
        except Exception as e:
            traceback.print_exc()
            summaries.append({
                'blog_id': blog['id'], 'blog_name': blog.get('name', ''),
                'results': [], 'summary': f'error: {e}',
            })
    return jsonify({'runs': summaries})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
