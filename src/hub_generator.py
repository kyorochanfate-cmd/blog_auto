"""トピッククラスター(ハブ記事)生成 + 公開キュー管理。

既存の公開記事を Gemini でテーマ別クラスタリングし、
各クラスタごとに「ピラー(ハブ)記事」を生成する。

ハブ記事の役割:
- 該当ジャンル全体を網羅する 5000〜8000字の総合解説
- クラスタ内の全スポーク記事へ内部リンクを集中
- Google から「このサイトはこのジャンルの権威」と評価されやすくする

スポーク記事側からハブへのリンク追加は別モジュール(hub_linker.py)で実装。
"""
import json
import re

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from webapp import blogs as blog_store


_CLUSTER_PROMPT = """以下は同じブログで公開済みの記事タイトル一覧です。これらをテーマ別に **3〜7個のクラスタ** に分類してください。

【クラスタリングのルール】
- 1クラスタあたり **最低{min_cluster}記事** を含むこと(少なすぎるテーマはまとめる or 除外)
- 各クラスタは「ハブ記事を1本書く価値があるテーマ」=広め (例: 「ARグラス」「ポータブル電源」「Switch 2 関連」「AI機能搭載デバイス」「イヤホン・ヘッドホン」など)
- 細かすぎる分け方は避ける (1記事しかないジャンルは「その他」に入れず除外でOK)
- クラスタ名は **検索されそうな日本語キーワード** で命名 (例: "ARグラス完全ガイド" のような長いものより "ARグラス" のようにシンプル)

【記事リスト (インデックス番号付き)】
{article_list}

【出力】
厳密なJSON形式のみ。前置き・コードフェンス禁止:
{{
  "clusters": [
    {{
      "theme": "クラスタ名 (15文字以内)",
      "description": "このテーマがどんなジャンルか1文で説明",
      "article_indices": [整数のリスト]
    }}
  ]
}}"""


_HUB_PROMPT = """あなたは経験豊富な日本のガジェットブロガーです。以下のテーマで「ピラー記事(ハブ記事)」を1本書いてください。

【テーマ】
{theme}

【テーマの説明】
{description}

【このハブ記事から内部リンクする既存記事 (スポーク記事)】
{articles_list}

【ハブ記事の役割】
- このテーマ全体を網羅する総合ガイド
- 上記の既存記事(スポーク)へ自然な文脈で内部リンクを集中させる
- 1記事で読者がこのテーマの全体像を把握できる

【記事の構成 (必ず守る)】
1. **# タイトル** — 「{theme}」を含む 28〜38文字。「完全ガイド」「徹底解説」「2026年版」「選び方」「おすすめ」などのハブ感を出すワードを1つ含める
2. **導入 (150〜200字)** — このテーマで何がわかるか3項目箇条書き
3. **`[:contents]`** を1行で書く (はてな目次自動生成)
4. **H2 セクション 4〜6個** — テーマの主要な切り口別に。各 H2 内で **既存記事を `[タイトル](URL)` 形式でリンク** すること
   - 例: 「## ARグラスの選び方の基本」セクション内で「詳しい比較は[XREAL One レビュー](URL)で解説しています」のように自然に挿入
5. **## 比較表** — 関連製品/選択肢 3〜5個を Markdown 表で
6. **## 関連記事一覧** — 上記の **全ての既存記事** を箇条書きでリンク
7. **## まとめ**
8. **## よくある質問** — `### Q. 〜` / `A. 〜` 形式で3問以上

【文字数】**5000〜8000字** (ピラー記事級の網羅性)

【絶対ルール】
- リンクは `[タイトル](URL)` の Markdown 形式のみ。URLは上記リストのものをそのまま使う(改変・推測禁止)
- 既存記事すべてに **最低1回はリンクする** (本文中 or 関連記事一覧で)
- 数値・スペック・価格は具体的に。確証ない数値は書かない
- 絵文字・記号アクセントは使わない
- 出力は Markdown のみ。前置き・コードフェンス不要

それでは記事を書いてください。"""


def cluster_articles_by_theme(blog_id, min_cluster=4):
    """公開済み記事を Gemini でテーマ別クラスタリング。

    Returns:
        {
            'total_articles': N,
            'clusters': [{'theme', 'description', 'articles': [{'title','url'},...]}, ...]
        }
    """
    articles = blog_store.list_published_articles(blog_id)
    if not articles:
        return {'total_articles': 0, 'clusters': []}

    article_list = '\n'.join(
        f'[{i}] {a["title"]}'
        for i, a in enumerate(articles)
    )
    prompt = _CLUSTER_PROMPT.format(
        min_cluster=min_cluster,
        article_list=article_list,
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        return {'total_articles': len(articles), 'clusters': [],
                'error': f'gemini failed: {e}'}

    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            return {'total_articles': len(articles), 'clusters': [],
                    'error': f'no JSON in response: {text[:200]}'}
        try:
            data = json.loads(m.group(0))
        except Exception:
            return {'total_articles': len(articles), 'clusters': [],
                    'error': f'JSON parse failed: {text[:200]}'}

    clusters = []
    for c in data.get('clusters', []):
        indices = c.get('article_indices', []) or []
        cluster_articles = [articles[i] for i in indices if 0 <= i < len(articles)]
        if len(cluster_articles) < min_cluster:
            continue
        clusters.append({
            'theme': c.get('theme', '(無題)'),
            'description': c.get('description', ''),
            'articles': [{'title': a['title'], 'url': a['url']} for a in cluster_articles],
        })

    return {'total_articles': len(articles), 'clusters': clusters}


def generate_hub_article(theme, description, articles, blog):
    """1クラスタからハブ記事 (markdown) を生成。

    Returns: (title, body) markdown
    """
    if not articles:
        return None, None

    articles_list = '\n'.join(
        f'- [{a["title"]}]({a["url"]})'
        for a in articles
    )

    prompt = _HUB_PROMPT.format(
        theme=theme,
        description=description or theme,
        articles_list=articles_list,
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.7),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        print(f'[hub-gen] gemini failed for {theme}: {e}', flush=True)
        return None, None

    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])

    # 1行目の # タイトル を抽出
    title = ''
    body = text
    m = re.match(r'^#\s+(.+?)\n([\s\S]*)$', text)
    if m:
        title = m.group(1).strip()
        body = m.group(2).strip()
    else:
        title = f'{theme} 完全ガイド 2026年版'
        body = text

    return title, body


_HUB_QUEUE_COLL = 'pending_hubs'


def _scrub_dead_internal_links(body, blog):
    """ハブ記事本文の Markdown 内部リンクを HEAD チェックして 404 を除去。

    Markdown `[text](url)` パターンを「text」に置換。
    箇条書きの場合は「- text」だけ残るので、リンクだけの箇条書き行ごと削除。
    """
    import re, requests
    domain = (blog.get('hatena_blog_domain') or '').strip()
    if not domain or not body:
        return body, 0
    pat = re.compile(r'\[([^\]]+)\]\((https?://' + re.escape(domain) + r'/entry/[^)]+)\)')
    matches = list(pat.finditer(body))
    if not matches:
        return body, 0

    # 重複URLは一度だけ HEAD チェック
    url_alive = {}
    def check(url):
        if url in url_alive:
            return url_alive[url]
        try:
            r = requests.head(url, timeout=8, allow_redirects=True)
            ok = r.status_code < 400
        except Exception:
            ok = False
        url_alive[url] = ok
        return ok

    dead_links = []  # [(orig_md, link_text)]
    for m in matches:
        text, url = m.group(1), m.group(2)
        if not check(url):
            dead_links.append((m.group(0), text))

    if not dead_links:
        return body, 0

    new_body = body
    for orig_md, link_text in dead_links:
        new_body = new_body.replace(orig_md, link_text)

    # 「- text」だけになった箇条書き行は削除 (内容としての価値なし)
    dead_texts = {t for _, t in dead_links}
    new_body = re.sub(
        r'^\s*[-*]\s+([^\n]+)\n',
        lambda m_: '' if m_.group(1).strip() in dead_texts else m_.group(0),
        new_body, flags=re.MULTILINE,
    )

    return new_body, len(dead_links)


def find_matching_hub(blog_id, topic_name, title=''):
    """記事タイトル/トピックに一致する公開済みハブを探す。

    マッチング: ハブの theme 文字列が title/topic_name に含まれるか、
    またはハブの spoke_urls の量で類似度を見る。

    Returns: {'title','url'} or None
    """
    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        client = firestore.Client()
        docs = list(
            client.collection(_HUB_QUEUE_COLL)
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .where(filter=FieldFilter('status', '==', 'published'))
            .stream()
        )
    except Exception as e:
        print(f'[hub-match] firestore failed: {e}', flush=True)
        return None

    if not docs:
        return None

    blob = f'{(topic_name or "").lower()} {(title or "").lower()}'

    # テーマ単語マッチ
    for d in docs:
        data = d.to_dict()
        theme = (data.get('theme') or '').lower()
        # 短すぎるテーマは部分マッチさせない (誤マッチ防止)
        if len(theme) < 4:
            continue
        # テーマがタイトル/トピック内に出現
        if theme in blob:
            url = data.get('published_url')
            if url:
                return {'title': data.get('title'), 'url': url}

    # マッチなし → 最新公開ハブを返す (汎用 fallback)
    docs_data = [d.to_dict() for d in docs]
    docs_data = [d for d in docs_data if d.get('published_url')]
    if not docs_data:
        return None
    docs_data.sort(key=lambda x: x.get('published_at') or 0, reverse=True)
    latest = docs_data[0]
    return {'title': latest.get('title'), 'url': latest.get('published_url')}


def queue_all_hubs(blog, min_cluster=4):
    """全クラスタのハブ記事を生成し Firestore キューに保存。

    article_count 降順 (記事数多いテーマほど価値が高いので先に公開) でキュー化。
    Returns: {'queued': N, 'themes': [...]}
    """
    blog_id = blog['id']
    cluster_result = cluster_articles_by_theme(blog_id, min_cluster=min_cluster)
    clusters = cluster_result.get('clusters') or []
    if not clusters:
        return {'queued': 0, 'error': cluster_result.get('error') or 'no clusters'}

    # 記事数の多い順
    clusters.sort(key=lambda c: -len(c['articles']))

    from google.cloud import firestore
    from datetime import datetime, timezone
    client = firestore.Client()

    queued = []
    for i, cluster in enumerate(clusters):
        title, body = generate_hub_article(
            cluster['theme'], cluster['description'], cluster['articles'], blog,
        )
        if not title:
            continue
        doc = {
            'blog_id': blog_id,
            'theme': cluster['theme'],
            'description': cluster['description'],
            'title': title,
            'body': body,
            'spoke_count': len(cluster['articles']),
            'spoke_urls': [a['url'] for a in cluster['articles']],
            'priority': i,  # 0 = 最優先 (記事数最多)
            'status': 'queued',
            'created_at': datetime.now(timezone.utc),
            'published_at': None,
            'published_url': None,
        }
        # ドキュメントID: blog_id_priority_theme (重複回避)
        import hashlib
        doc_id = hashlib.sha256(
            f"{blog_id}:{cluster['theme']}".encode('utf-8')
        ).hexdigest()[:24]
        client.collection(_HUB_QUEUE_COLL).document(doc_id).set(doc)
        queued.append({'doc_id': doc_id, 'theme': cluster['theme'],
                       'spoke_count': len(cluster['articles']),
                       'title': title, 'body_length': len(body)})

    return {'queued': len(queued), 'themes': queued}


def get_queued_hubs(blog_id):
    """ブログの待機中ハブ一覧を priority 昇順で取得。"""
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    client = firestore.Client()
    docs = (client.collection(_HUB_QUEUE_COLL)
            .where(filter=FieldFilter('blog_id', '==', blog_id))
            .where(filter=FieldFilter('status', '==', 'queued'))
            .stream())
    items = []
    for d in docs:
        data = d.to_dict()
        items.append({
            'doc_id': d.id,
            'theme': data.get('theme'),
            'title': data.get('title'),
            'spoke_count': data.get('spoke_count'),
            'priority': data.get('priority', 99),
            'created_at': data.get('created_at'),
            'body_length': len(data.get('body') or ''),
        })
    items.sort(key=lambda x: x.get('priority', 99))
    return items


def publish_next_hub(blog):
    """キューから最優先1件をはてなブログに公開。

    Cloud Scheduler から日次で叩く想定。
    Returns: {'ok','published_url','title','theme'} or {'skipped':'no_queue'}
    """
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    from datetime import datetime, timezone

    blog_id = blog['id']
    client = firestore.Client()
    # priority 昇順で1件取得
    docs = list(
        client.collection(_HUB_QUEUE_COLL)
        .where(filter=FieldFilter('blog_id', '==', blog_id))
        .where(filter=FieldFilter('status', '==', 'queued'))
        .stream()
    )
    if not docs:
        return {'skipped': 'no_queue'}

    docs_data = [(d.id, d.to_dict()) for d in docs]
    docs_data.sort(key=lambda x: x[1].get('priority', 99))
    doc_id, data = docs_data[0]
    title = data['title']
    body = data['body']

    # 公開直前に本文中の内部リンクを HEAD チェック、404 のリンクを除去
    # (ハブを5/22にキューに積んだ後、5/23 に81記事削除した結果、
    #  spoke_urls の中に死リンクが大量に残っている問題への対策)
    body, dead_count = _scrub_dead_internal_links(body, blog)
    if dead_count > 0:
        print(f'[hub-publish] removed {dead_count} dead links before publishing', flush=True)

    from src.hatena_publisher import publish as hatena_publish
    from src.structured_data import build_jsonld, append_jsonld
    from src import google_indexing

    site_url = f'https://{blog["hatena_blog_domain"]}' if blog.get('hatena_blog_domain') else ''
    try:
        jsonld = build_jsonld(title, body, kind='article',
                              author_name=blog.get('hatena_id', ''), site_url=site_url)
        body_with_schema = append_jsonld(body, jsonld)
    except Exception:
        body_with_schema = body

    banners = (blog.get('hatena_group_banners') or '').strip()
    if banners and banners not in body_with_schema:
        body_with_schema = body_with_schema.rstrip() + '\n\n' + banners + '\n'

    try:
        url = hatena_publish(blog, title, body_with_schema)
    except Exception as e:
        return {'ok': False, 'error': f'publish failed: {e}', 'theme': data.get('theme')}

    # 記録 + キュー更新
    try:
        blog_store.record_published_article(blog_id, title, body, url)
    except Exception:
        pass
    try:
        google_indexing.notify_url(url)
    except Exception:
        pass

    client.collection(_HUB_QUEUE_COLL).document(doc_id).update({
        'status': 'published',
        'published_at': datetime.now(timezone.utc),
        'published_url': url,
    })

    return {
        'ok': True, 'theme': data.get('theme'),
        'title': title, 'published_url': url,
        'spoke_count': data.get('spoke_count'),
        'remaining_in_queue': len(docs_data) - 1,
    }


def run_hub_pipeline(blog, min_cluster=4, dry_run=False):
    """全クラスタを処理してハブ記事を生成 (+ dry_run なら投稿せずプレビュー)。

    Returns: 各クラスタごとの結果サマリ
    """
    blog_id = blog['id']
    cluster_result = cluster_articles_by_theme(blog_id, min_cluster=min_cluster)
    if cluster_result.get('error'):
        return {'ok': False, 'error': cluster_result['error']}
    if not cluster_result.get('clusters'):
        return {'ok': False, 'error': 'no clusters found'}

    results = []
    for cluster in cluster_result['clusters']:
        title, body = generate_hub_article(
            cluster['theme'], cluster['description'], cluster['articles'], blog,
        )
        if not title:
            results.append({
                'theme': cluster['theme'],
                'ok': False, 'error': 'hub generation failed',
                'article_count': len(cluster['articles']),
            })
            continue

        item = {
            'theme': cluster['theme'],
            'description': cluster['description'],
            'article_count': len(cluster['articles']),
            'hub_title': title,
            'hub_body_length': len(body),
            'hub_body_preview': body[:800],
            'spokes': [a['url'] for a in cluster['articles']],
        }
        if dry_run:
            item['ok'] = True
            item['dry_run'] = True
            item['hub_body_full'] = body
        else:
            # 本番投稿: Hatena AtomPub publish + record
            try:
                from src.hatena_publisher import publish as hatena_publish
                from src.structured_data import build_jsonld, append_jsonld
                site_url = f'https://{blog["hatena_blog_domain"]}' if blog.get('hatena_blog_domain') else ''
                # JSON-LD
                jsonld = build_jsonld(title, body, kind='article',
                                       author_name=blog.get('hatena_id', ''), site_url=site_url)
                body_with_schema = append_jsonld(body, jsonld)
                # バナー
                banners = (blog.get('hatena_group_banners') or '').strip()
                if banners and banners not in body_with_schema:
                    body_with_schema = body_with_schema.rstrip() + '\n\n' + banners + '\n'
                hub_url = hatena_publish(blog, title, body_with_schema)
                blog_store.record_published_article(blog_id, title, body, hub_url)
                # Google Indexing 通知
                try:
                    from src import google_indexing
                    google_indexing.notify_url(hub_url)
                except Exception:
                    pass
                item['ok'] = True
                item['hub_url'] = hub_url
            except Exception as e:
                item['ok'] = False
                item['error'] = f'publish failed: {e}'
        results.append(item)

    return {
        'ok': True,
        'total_articles': cluster_result['total_articles'],
        'cluster_count': len(cluster_result['clusters']),
        'results': results,
    }
