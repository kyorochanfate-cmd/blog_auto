"""記事ライティングを「準備 → 待機キュー → 本文受領 → 仕上げ」に分離する仕組み。

目的:
- 本文の生成だけを Claude Code scheduled agent (Opus) に任せる
- それ以外の処理 (RSS収集・トピック選定・画像処理・商品カード・投稿) は Gemini Flash Lite + Cloud Run のまま

フロー:
  1. Cloud Run /auto-run が起動 → prepare_pending_write() で Firestore に saving
  2. Claude Code agent が /admin/next-pending-write で取得
  3. agent が Opus で本文執筆 → /admin/submit-written-body に POST
  4. submit_written_body() が finalize_with_body() を呼んで仕上げ・公開
  5. (保険) 30分以上 pending のものは Gemini で書いて自動公開する fallback

Firestore コレクション: `pending_writes`
  doc id = sha256(blog_id:topic_name:created_at)[:32]
  fields:
    blog_id: str
    topic_name, topic_summary: str
    prompt: str (フルプロンプト、agent に渡す)
    sources: list (参考記事)
    wiki_images, official_image: dict
    related_articles, longtail_keywords: list
    use_product_cards: bool
    article_type: str (A-G)
    status: 'pending' | 'writing' | 'published' | 'failed' | 'fallback_published'
    created_at: datetime
    claimed_at: datetime (status=writing になった時刻)
    finished_at: datetime
    published_url: str
    error: str
"""
import hashlib
from datetime import datetime, timezone, timedelta

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter


_COLL = 'pending_writes'


def _client():
    return firestore.Client()


def _doc_id(blog_id, topic_name):
    now = datetime.now(timezone.utc).isoformat()
    raw = f'{blog_id}:{topic_name}:{now}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def save_pending(blog_id, topic_name, topic_summary, prompt, context_dict):
    """準備済みのライティングタスクを Firestore に保存する。

    context_dict には sources, wiki_images, official_image, related_articles,
    longtail_keywords, use_product_cards, article_type 等を入れる。
    """
    doc_id = _doc_id(blog_id, topic_name)
    now = datetime.now(timezone.utc)
    doc = {
        'blog_id': blog_id,
        'topic_name': topic_name,
        'topic_summary': topic_summary,
        'prompt': prompt,
        'context': context_dict,  # finalize で再利用するためにフル context を保存
        'status': 'pending',
        'created_at': now,
        'claimed_at': None,
        'finished_at': None,
        'published_url': None,
        'error': None,
    }
    _client().collection(_COLL).document(doc_id).set(doc)
    return doc_id


def claim_next_pending(blog_id=None):
    """status=pending の最古ドキュメントを取得し、status を 'writing' に更新。

    Claude Code agent から叩かれる前提。
    blog_id 指定で特定ブログだけ対象に絞れる。
    Returns: doc dict + 'doc_id' or None
    """
    client = _client()
    coll = client.collection(_COLL)
    q = coll.where(filter=FieldFilter('status', '==', 'pending'))
    if blog_id:
        q = q.where(filter=FieldFilter('blog_id', '==', blog_id))
    docs = list(q.stream())
    if not docs:
        return None
    docs_data = [(d.id, d.to_dict()) for d in docs]
    # created_at 昇順 = 最古優先
    docs_data.sort(key=lambda x: x[1].get('created_at') or datetime.max.replace(tzinfo=timezone.utc))
    doc_id, data = docs_data[0]
    # claim
    coll.document(doc_id).update({
        'status': 'writing',
        'claimed_at': datetime.now(timezone.utc),
    })
    return {'doc_id': doc_id, **data}


def get_pending(doc_id):
    client = _client()
    doc = client.collection(_COLL).document(doc_id).get()
    if not doc.exists:
        return None
    return {'doc_id': doc.id, **doc.to_dict()}


def mark_published(doc_id, url, title=''):
    _client().collection(_COLL).document(doc_id).update({
        'status': 'published',
        'finished_at': datetime.now(timezone.utc),
        'published_url': url,
        'final_title': title,
    })


def mark_failed(doc_id, error):
    _client().collection(_COLL).document(doc_id).update({
        'status': 'failed',
        'finished_at': datetime.now(timezone.utc),
        'error': str(error)[:500],
    })


def list_stale_pending(stale_minutes=30, status_filter=None):
    """指定分以上経過して未完了のドキュメントを取得 (fallback対象)。

    status_filter: ['pending'] (まだ取られていない) or ['writing'] (取られたが完了せず)
                   None なら両方
    """
    if status_filter is None:
        status_filter = ['pending', 'writing']
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    client = _client()
    coll = client.collection(_COLL)
    out = []
    for status in status_filter:
        q = coll.where(filter=FieldFilter('status', '==', status))
        for d in q.stream():
            data = d.to_dict()
            ts = data.get('created_at')
            if ts and ts < cutoff:
                out.append({'doc_id': d.id, **data})
    out.sort(key=lambda x: x.get('created_at') or datetime.max.replace(tzinfo=timezone.utc))
    return out


def count_by_status(blog_id=None):
    """ステータス別の件数を返す (運用監視用)。"""
    client = _client()
    coll = client.collection(_COLL)
    q = coll
    if blog_id:
        q = coll.where(filter=FieldFilter('blog_id', '==', blog_id))
    counts = {'pending': 0, 'writing': 0, 'published': 0, 'failed': 0, 'fallback_published': 0}
    for d in q.stream():
        st = (d.to_dict().get('status') or '?')
        counts[st] = counts.get(st, 0) + 1
    return counts
