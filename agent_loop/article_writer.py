"""C: ソース記事本文を読んで、定型 angle で切り込む記事執筆 → ③投稿待ち。

設計（焼き直し防止フェーズ）:
1. 外部トレンドからタイトル候補を選ぶ際、Gemini に **ソース記事3本のインデックス**
   と **定型 angle 1つ** も同時に選ばせる
2. そのソース記事3本の本文を trafilatura で fetch（一次情報の代用）
3. 本文プロンプトで「これらソースを読んだ上で、angle に沿って、ソースに無い
   切り口で書け」と指示
4. 自己採点は「中身」軸（数値・出典・独自洞察・angle 活用・行動喚起）に変更
5. ⑤フィードバックを毎回注入
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

from google import genai

from . import sheets, trend_signals

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


# 焼き直し防止のための「定型 angle」ライブラリ。
# AI に「独自の切り口を考えろ」は無理なので、テンプレを与えて選ばせる。
_ANGLES = {
    'cost_3yr':
        '3年使い続けた場合のトータルコスト試算（本体・サブスク・周辺・電気代・買い替え）を中心に書く',
    'not_for_beginners':
        '初心者・ライトユーザーには絶対勧めない理由3つを軸に、誰のための製品かを限定的に定義する',
    'cheap_alternative':
        '価格半分の代替品を1つ取り上げ、実用差がどこに出るか・出ないかを具体的に比較する',
    'maker_hidden_flaws':
        'メーカー公式が言わない / 言いたがらない欠点3つを軸に、購入前の心得を書く',
    'daily_scene':
        '朝の通勤・職場・自宅作業・休日 のうち2シーン以上について、具体的な使い方を時系列で描写する',
    'competitive_rank':
        '同価格帯の競合3-5機種と比較し、項目別に1〜N位を順位付けする（ランキング記事）',
    'future_proof':
        '今後1〜2年のアップデートで化ける可能性 vs 塩漬けリスクを予想し、買い時を提言する',
    'total_cost':
        'サブスク・周辺機器・電気代・買い替え周期まで含めた「本当の価格」を試算し、表示価格との乖離を示す',
    'resale':
        '中古売却時のリセールバリュー視点で、損切りラインを計算する',
    'use_unintended':
        '本来想定されていない/メーカーが宣伝していない使い方を発見・提案する',
}


# ──────────────── プロンプト ────────────────

_TITLE_PROMPT = """あなたは私のガジェットブログの編集長です。明日書くべき記事を1本選び、ソースと切り口も指定してください。

# ブログの方針（自社の傾向分析から）
{policy}

# 外部で話題になっているトピック（はてブ/Reddit/HN 直近72時間。各行 [N] は index）
{trending}

# 過去に既に書いた記事（重複NG）
{existing_titles}

# 読者フィードバック（過去にあなたが気づいた問題点。必ず反映）
{feedback}

# 利用可能な「切り口（angle）」（このどれか1つを必ず選ぶ）
{angles}

# 出力ルール
- 上記トレンドから書ける題材を選ぶ
- 過去記事と重複しない
- 「読者の検索意図が明確で、解決したい欲求があるもの」を選ぶ
- angle は「題材 × angle」の組み合わせが面白くなるものを選ぶ
- source_indices には、その題材を書くのに参考にする trending 上の index を最大3つ選ぶ
  （関連が薄ければ少なくても可）

# 出力形式（JSON、コードフェンスなし）
{{
  "title": "...",
  "type": "review|comparison|news|howto|guide",
  "angle_key": "_ANGLES のキー名（cost_3yr 等）",
  "search_intent": "読者が検索する具体的な意図を1行で",
  "source_indices": [N, N, N],
  "rationale": "なぜこのタイトル×angle が読者にとって価値があるか1行で"
}}
"""


_BODY_PROMPT = """あなたは私のガジェットブログの執筆者です。「焼き直し」と「平均的見解の言い直し」を絶対に避け、読者が他では得られない記事を書いてください。

# タイトル
{title}

# 記事タイプ
{article_type}

# 切り口（必ずこの angle を記事の主軸に据える）
{angle}

# 検索意図
{search_intent}

# 参考にした外部ソース（これらを読んだ上で「書かれていない切り口」で書く）
{sources}

# 過去公開記事（内部リンク素材、関連性高いもの2-3本を本文中盤〜終盤に [タイトル](URL) で挿入）
{past_articles}

# 読者フィードバック（前回までの指摘。必ず反映）
{feedback}

# 必須ルール
- 文字数: 2000〜3500字
- 上記ソースを参考にしつつ、**ソースをそのまま要約しない**。ソースに書いていない切り口を最低3つ含める
- 切り口（angle）を冒頭で宣言し、全節でそれに沿って書く（雑多な総合解説にしない）
- 数値は出典明示（メーカー公式 / 報道記事 / 仕様書）。不明は「未公表」と明記。捏造禁止
- 実機所有・実機使用の主張は禁止。「公表スペックから見ると」「仕様上は」のように仕様ベースで
- 「便利です」「快適です」「魅力的です」のような抽象的褒め言葉を最後の手段にする
- 構成（タイプによらず厳守）:
  1. 冒頭【3行まとめ】（40字×3行）
  2. 「この記事の立場（angle）」を1段落で宣言
  3. 本論（angle に従って書く）
  4. 末尾【FAQ】（Q&A 3つ）
  5. 末尾【出典】（参考にした公式情報・記事のURL列挙、必須）

# 商品カード（記事と無関係なカードは絶対に置かない）
- 形式: `[PRODUCT_CARD: 商品名]` を独立行で置く。目安2〜4個
- **本文で実際に名前を出して論じた製品のみ**カードにする。文中に一度も出てこない製品のカードは禁止
- カードは、その製品を論じた段落の直後に置く（無関係な位置に固めない）
- 商品名は「メーカー名 + 型番」の形式にする（例: `Sony WH-1000XM5` / `Anker PowerCore 10000`）
- **すでに発売済みで、日本国内で本体が実売されている製品に限る**。
  未発表・未発売・噂段階の製品（次期モデルの予想型番など）はカードにしない
  （本体が売られていないと、ケースやフィルムなどの無関係な商品しかヒットしないため）
- 「スマートフォン」「ワイヤレスイヤホン」のような一般名詞だけのカードは禁止（型番を必ず含める）
- 適切な製品が2つ未満なら、無理に数を揃えず1つでよい

# 出力
Markdown本文のみ。タイトル行は含めない。前置きや「```」は不要。

本文:
"""


_CRITIQUE_PROMPT = """以下は私のガジェットブログの記事下書きです。読者目線で**中身**を厳しく評価してください。
書式や読みやすさは二の次。「読み終わって、ネットで他に読める内容と何が違ったか」が問われます。

# タイトル
{title}

# 想定した切り口（この angle で書くべきだった）
{angle}

# 本文
{body}

# 評価軸（各10点満点）
1. specific_numbers: 具体的な数値（価格・スペック・期間・%）が3つ以上 自然に登場するか
2. cited_sources: 出典・ソースが本文中で明示されているか（メーカー公式・記事URL等）
3. unique_insight: 一般的なネット情報の要約に留まらず、ソースに無かった独自の洞察が1つ以上あるか
4. angle_consistency: 指定の angle が記事全体の主軸として活きているか（angle と無関係な総花的記述は減点）
5. actionable: 読者が記事を読み終えて「次にどう動くか」が明確に書かれているか（買う/見送る/比較する/別記事を読む など）

# 出力形式（JSON、コードフェンスなし）
{{"scores": {{"specific_numbers": N, "cited_sources": N, "unique_insight": N, "angle_consistency": N, "actionable": N}}, "total": N, "weak_points": ["改善点1", "改善点2", ...], "rewrite_hints": "書き直し時に必ず守ること1行"}}

total は各項目の平均（小数点1桁）。
"""

_REWRITE_PROMPT_SUFFIX = """

# 前回の自己評価で出た改善点（必ず反映して書き直す）
{weak_points}

# 書き直し時の必須指示
{rewrite_hints}
"""


# ──────────────── ユーティリティ ────────────────

def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith('```'):
        lines = s.splitlines()
        if lines[-1].startswith('```'):
            s = '\n'.join(lines[1:-1])
        else:
            s = '\n'.join(lines[1:])
    return s.strip()


def _gemini_json(client, model: str, prompt: str) -> dict:
    resp = client.models.generate_content(model=model, contents=prompt)
    text = _strip_json_fences(resp.text or '')
    return json.loads(text)


def _format_past_articles(past: list[dict]) -> str:
    if not past:
        return '(まだ公開済み記事なし)'
    return '\n'.join(f"- [{a['title']}]({a['url']})" for a in past[:30])


def _format_feedback(items: list[dict]) -> str:
    if not items:
        return '(まだフィードバックなし)'
    lines = []
    for it in items:
        target = it.get('target', '')
        fb = it.get('feedback', '')
        if target:
            lines.append(f'- 【{target}】 {fb}')
        else:
            lines.append(f'- {fb}')
    return '\n'.join(lines)


def _format_angles() -> str:
    return '\n'.join(f'- {k}: {v}' for k, v in _ANGLES.items())


def _format_sources(sources: list[dict]) -> str:
    if not sources:
        return '(ソース取得に失敗。一般的知識から書いてください、ただし焼き直しに陥らないよう注意)'
    lines = []
    for i, s in enumerate(sources):
        lines.append(f"## ソース{i+1}: {s['title']}\nURL: {s['url']}\n本文（抜粋）:\n{s['text']}\n")
    return '\n'.join(lines)


# ──────────────── タイトル選定（メタ情報込み） ────────────────

def _pick_title_with_meta(
    policy: str, existing: list[str],
    trending: list[dict], feedback: list[dict],
) -> dict:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')

    prompt = _TITLE_PROMPT.format(
        policy=policy or '(なし)',
        trending=trend_signals.format_for_prompt(trending, limit=25),
        existing_titles='\n'.join(f'- {t}' for t in existing[-30:]) or '(なし)',
        feedback=_format_feedback(feedback),
        angles=_format_angles(),
    )

    try:
        data = _gemini_json(client, model, prompt)
    except Exception as e:
        log.warning('Title JSON parse failed, falling back: %s', e)
        return {
            'title': '', 'type': 'review', 'angle_key': 'maker_hidden_flaws',
            'search_intent': '', 'source_indices': [], 'rationale': '',
        }

    title = (data.get('title') or '').strip().lstrip('・-*0123456789. 　')
    angle_key = (data.get('angle_key') or 'maker_hidden_flaws').strip()
    if angle_key not in _ANGLES:
        angle_key = 'maker_hidden_flaws'

    return {
        'title': title[:80],
        'type': (data.get('type') or 'review').strip().lower(),
        'angle_key': angle_key,
        'search_intent': (data.get('search_intent') or '').strip(),
        'source_indices': data.get('source_indices') or [],
        'rationale': (data.get('rationale') or '').strip(),
    }


# ──────────────── 本文生成 + 自己採点 + 書き直し ────────────────

def _write_body_once(
    title: str, article_type: str, angle_text: str, search_intent: str,
    sources: list[dict], past_articles: list[dict], feedback: list[dict],
    rewrite_context: dict | None = None,
) -> str:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')

    prompt = _BODY_PROMPT.format(
        title=title,
        article_type=article_type,
        angle=angle_text,
        search_intent=search_intent or '(未指定)',
        sources=_format_sources(sources),
        past_articles=_format_past_articles(past_articles),
        feedback=_format_feedback(feedback),
    )
    if rewrite_context:
        prompt += _REWRITE_PROMPT_SUFFIX.format(
            weak_points='\n'.join(f'- {p}' for p in rewrite_context.get('weak_points', [])),
            rewrite_hints=rewrite_context.get('rewrite_hints', ''),
        )

    resp = client.models.generate_content(model=model, contents=prompt)
    body = (resp.text or '').strip()
    body = _strip_json_fences(body) if body.startswith('```') else body
    if len(body) < 500:
        raise RuntimeError(f'Generated body too short ({len(body)} chars)')
    return body


def _self_critique(title: str, angle_text: str, body: str) -> dict:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    prompt = _CRITIQUE_PROMPT.format(title=title, angle=angle_text, body=body[:6000])
    try:
        data = _gemini_json(client, model, prompt)
        if 'total' not in data and 'scores' in data:
            vals = list(data['scores'].values())
            data['total'] = round(sum(vals) / max(len(vals), 1), 1)
        return data
    except Exception as e:
        log.warning('critique JSON parse failed: %s', e)
        return {'total': 10.0, 'weak_points': [], 'rewrite_hints': ''}


def _write_with_quality_gate(
    title: str, article_type: str, angle_text: str, search_intent: str,
    sources: list[dict], past_articles: list[dict], feedback: list[dict],
    threshold: float = 7.0,
) -> tuple[str, dict]:
    body = _write_body_once(
        title, article_type, angle_text, search_intent,
        sources, past_articles, feedback,
    )
    log.info('Generated body v1 (%d chars)', len(body))

    critique = _self_critique(title, angle_text, body)
    log.info('Self-critique v1: total=%s scores=%s', critique.get('total'), critique.get('scores'))

    if float(critique.get('total', 10)) >= threshold:
        return body, critique

    log.info('Below threshold (%.1f), rewriting once...', threshold)
    body2 = _write_body_once(
        title, article_type, angle_text, search_intent,
        sources, past_articles, feedback,
        rewrite_context=critique,
    )
    log.info('Generated body v2 (%d chars)', len(body2))

    critique2 = _self_critique(title, angle_text, body2)
    log.info('Self-critique v2: total=%s', critique2.get('total'))

    if float(critique2.get('total', 0)) >= float(critique.get('total', 0)):
        return body2, critique2
    return body, critique


# ──────────────── エントリーポイント ────────────────

def run() -> str | None:
    sheets.ensure_headers()

    policy = sheets.read_policy()
    existing = sheets.list_queue_titles()
    past_articles = sheets.list_published(limit=30)
    feedback = sheets.read_feedback(limit=10)
    trending = trend_signals.fetch_trending(per_source=6, hours=72)

    log.info(
        'inputs: policy=%dch existing=%d past=%d feedback=%d trending=%d',
        len(policy), len(existing), len(past_articles), len(feedback), len(trending),
    )

    meta = _pick_title_with_meta(policy, existing, trending, feedback)
    title = meta['title']
    if not title:
        raise RuntimeError('No title picked')
    if title in existing:
        log.warning('Title already in queue: %s. Skipping.', title)
        return None

    angle_text = _ANGLES.get(meta['angle_key'], _ANGLES['maker_hidden_flaws'])
    log.info(
        'Picked title=%r type=%s angle=%s intent=%s sources=%s',
        title, meta['type'], meta['angle_key'],
        meta['search_intent'], meta['source_indices'],
    )

    # ソース記事本文を取得（成功した分だけ使う）
    sources = trend_signals.fetch_source_bodies(trending, meta['source_indices'])
    log.info('Fetched %d source bodies', len(sources))

    body, critique = _write_with_quality_gate(
        title=title,
        article_type=meta['type'],
        angle_text=angle_text,
        search_intent=meta['search_intent'],
        sources=sources,
        past_articles=past_articles,
        feedback=feedback,
    )
    log.info(
        'Final critique total=%s weak_points=%s',
        critique.get('total'), critique.get('weak_points'),
    )

    now = datetime.now(JST)
    article_id = now.strftime('%Y%m%d-%H%M%S')
    sheets.append_queue_article(
        article_id=article_id,
        created_at=now.strftime('%Y-%m-%d %H:%M:%S'),
        title=title,
        body_md=body,
    )
    log.info('Appended to queue: id=%s title=%s', article_id, title)
    return title
