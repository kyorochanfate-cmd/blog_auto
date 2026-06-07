"""Search Console データに基づく記事リライト。

実検索クエリ(読者が実際に打ち込んだ語)に対して既存記事の
タイトル+導入文が刺さっていないケースを発見し、Geminiで書き直す。

フロー:
  1. GSC から記事ごと・クエリごとの実績を取得
  2. インプ多い×CTR低い×順位そこそこ良い記事 = リライト候補
  3. 候補ページの記事を Hatena AtomPub から取得
  4. Gemini にターゲットクエリを伝えてタイトル + 導入文を書き直し
  5. PUT で記事更新 → Google Indexing API に通知
"""
import re

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from src import search_console
from src import affiliate_upgrade  # _list_entries / fetch_entry / update_entry を流用
from src import google_indexing


_REWRITE_PROMPT = """以下の既存記事のタイトルと導入文を、ターゲット検索クエリ「{query}」に強く刺さるように書き直してください。

【現状】
- このページは「{query}」というクエリでGoogle検索結果に {position:.1f} 位で表示されているが、CTR が {ctr_pct:.1f}% と低い。
- インプレッション数: {impressions} / クリック数: {clicks}
- 仮説: タイトルが「{query}」と検索する人の悩み・検索意図とズレているため CTR が伸びていない。

【ターゲットクエリ】
{query}

【現在のタイトル】
{title}

【現在の本文 (先頭1500字)】
{intro}

【書き直しルール】
- **タイトル**: 28〜38文字。**ターゲットクエリ「{query}」を必ず左寄せで含める**。読者の悩み・知りたいこと(「{query}」を打ち込んだ人の心理)に直結する課題解決型に。
- 「[実機比較]」「[最新ニュース]」のような括弧タイトルは禁止
- 数字 or 年号 or 比較系ワード(vs/違い/比較/おすすめ/レビュー/2026年版) を含めると尚良し
- **導入文** (記事冒頭 約200文字): 検索クエリ「{query}」をすぐに想起させる1文目から始める。「この記事でわかること」を3項目箇条書きに含める
- **本文の構造・H2見出し・FAQ・装飾・画像は変更しない**。タイトル行と「導入文(最初のH2より前の段落)」だけ書き換える
- 既存の `[:contents]` `[CHART]` `![]()` などのマーカーは維持

【出力形式】
JSON のみ。前置き・コードフェンス禁止。
{{"title": "新タイトル", "intro": "新導入文(マークダウン形式、改行込み)"}}
"""


def rewrite_article_for_query(blog, page_url, query, position, ctr, impressions, clicks, dry_run=False):
    """1記事をターゲットクエリで書き直す。

    Returns: {'ok','old_title','new_title','old_intro','new_intro','url'} など
    """
    # ページURLから記事を探す
    entry_meta = _find_entry_by_url(blog, page_url)
    if not entry_meta:
        return {'ok': False, 'error': f'entry not found for url {page_url}'}

    entry = affiliate_upgrade.fetch_entry(blog, entry_meta['edit_url'])
    body = entry['content'] or ''
    title = entry['title']

    # 導入文 = 最初のH2より前のテキスト全部
    intro_match = re.search(r'^(.*?)(?=^##\s)', body, re.DOTALL | re.MULTILINE)
    if not intro_match:
        return {'ok': False, 'error': 'no intro section detected (no H2 found)'}
    old_intro = intro_match.group(1).rstrip()
    rest = body[len(old_intro):]

    # Gemini に問い合わせ
    prompt = _REWRITE_PROMPT.format(
        query=query,
        position=position,
        ctr_pct=ctr * 100,
        impressions=impressions,
        clicks=clicks,
        title=title,
        intro=old_intro[:1500],
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        return {'ok': False, 'error': f'gemini failed: {e}'}

    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
    import json as _json
    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            return {'ok': False, 'error': f'no JSON in gemini response: {text[:200]}'}
        try:
            data = _json.loads(m.group(0))
        except Exception:
            return {'ok': False, 'error': f'JSON parse failed: {text[:200]}'}

    new_title = (data.get('title') or '').strip()
    new_intro = (data.get('intro') or '').strip()
    if not (new_title and new_intro):
        return {'ok': False, 'error': 'gemini returned empty title or intro'}

    # 新しい body を組み立てる: 新導入文 + (改行) + 既存の H2 以降
    new_body = new_intro.rstrip() + '\n\n' + rest.lstrip()

    if dry_run:
        return {
            'ok': True, 'dry_run': True,
            'url': page_url, 'query': query,
            'old_title': title, 'new_title': new_title,
            'old_intro_len': len(old_intro),
            'new_intro_len': len(new_intro),
            'old_intro_preview': old_intro[:300],
            'new_intro_preview': new_intro[:300],
        }

    affiliate_upgrade.update_entry(blog, entry_meta['edit_url'], new_title, new_body, entry['categories'])

    # Google Indexing API でクロール促進
    try:
        google_indexing.notify_url(page_url)
    except Exception as e:
        print(f'[rewriter] indexing notify failed: {e}', flush=True)

    return {
        'ok': True,
        'url': page_url, 'query': query,
        'old_title': title, 'new_title': new_title,
        'old_intro_len': len(old_intro),
        'new_intro_len': len(new_intro),
    }


def _find_entry_by_url(blog, target_url):
    """ページURLからエントリを検索 (最大30ページ走査 = 約200記事)。"""
    target_url = (target_url or '').rstrip('/')
    page_url = None
    for _ in range(30):
        entries, next_link = affiliate_upgrade._list_entries(blog, page_url)
        for e in entries:
            if (e.get('alternate_url') or '').rstrip('/') == target_url:
                return e
        if not next_link:
            break
        page_url = next_link
    return None


def run_rewrite_pipeline(blog, days=28, top_n=3, min_impressions=30, dry_run=False):
    """GSCデータ取得 → 候補抽出 → 上位 N 件をリライト。

    Returns:
        {
            'ok': bool,
            'fetched_rows': int,
            'candidates': [...],
            'processed': [...],   # 実際に書き直したもの (dry_runでもプレビューが入る)
        }
    """
    domain = (blog.get('hatena_blog_domain') or '').strip()
    if not domain:
        return {'ok': False, 'error': 'no hatena_blog_domain in blog config'}
    site_url = f'https://{domain}/'

    # 1. GSCから検索クエリ実績を取得
    sc_result = search_console.query_pages(site_url, days=days, row_limit=2000)
    if not sc_result.get('ok'):
        return {'ok': False, 'error': f'GSC fetch failed: {sc_result.get("error")} {sc_result.get("detail","")}'}
    rows = sc_result['rows']

    # 2. リライト候補を抽出
    candidates = search_console.find_rewrite_candidates(
        rows,
        min_impressions=min_impressions,
        ctr_threshold=0.02,
        position_max=20,
    )

    # 3. top_n をリライト
    processed = []
    for cand in candidates[:top_n]:
        r = rewrite_article_for_query(
            blog,
            page_url=cand['page'],
            query=cand['query'],
            position=cand['position'],
            ctr=cand['ctr'],
            impressions=cand['impressions'],
            clicks=cand['clicks'],
            dry_run=dry_run,
        )
        r['score'] = cand['score']
        processed.append(r)

    return {
        'ok': True,
        'fetched_rows': len(rows),
        'candidates_count': len(candidates),
        'candidates_preview': candidates[:10],
        'processed': processed,
        'date_range': f'{sc_result.get("start_date","?")} 〜 {sc_result.get("end_date","?")}',
    }
