"""C: 旬のトピック + ②方針 + ⑤フィードバック → Gemini で記事執筆 → ③投稿待ち。

設計（種まき期向け）:
1. 自社GA4データは薄いので、外部トレンド（はてブ/Reddit/HN）を主要な題材源にする
2. ②方針も参照するが、固定の10章構成は廃止
3. タイトルから「記事タイプ」を判定し、タイプ別に最適な構成を Gemini に指示
4. ⑤フィードバック（あなたが書いた主観メモ）を必ずプロンプトに注入
5. 書いた記事を Gemini に自己採点させ、7点未満なら1回だけ書き直し
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from google import genai

from . import sheets, trend_signals

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


# ──────────────── プロンプト ────────────────

_TITLE_PROMPT = """あなたは私のガジェットブログの編集長です。明日書くべき記事タイトルを1本だけ選んでください。

# ブログの方針（自社の傾向分析から）
{policy}

# いま外部で話題になっているトピック（はてブ/Reddit/HN 直近72時間）
{trending}

# 過去に既に書いた記事（重複NG）
{existing_titles}

# 読者フィードバック（過去にあなたが気づいた問題点）
{feedback}

# 選定基準
1. 上記の話題から、私のブログ「ガジェット × 仕事効率／日常生活」の文脈で書けるもの
2. 過去記事と重複しない
3. 読者フィードバックの指摘を踏まえる
4. 検索意図が明確で、読者が知りたい/解決したい欲求がはっきりしているもの
5. 「◯◯ってどう？」のような曖昧なものでなく、「◯◯と△△、どっちがいいか」「◯◯で失敗しない3つの注意点」のような具体的な切り口

# 出力形式（JSON、コードフェンスなし）
{{"title": "...", "type": "review|comparison|news|howto|guide", "search_intent": "読者が検索する具体的な意図を1行で", "rationale": "なぜこのタイトルなのか1行で"}}
"""

# 記事タイプ別の構成指示
_STRUCTURE_BY_TYPE = {
    'review':
        '構成: ## 結論：誰におすすめ/不要 → ## 良いところ3点 → '
        '## 気になるところ3点（必ず欠点も書く） → ## 競合との違い → '
        '## こんな人なら買い → ## FAQ',
    'comparison':
        '構成: ## 結論：◯◯派ならA、△△派ならB → ## 比較表（必ずMarkdown表で） → '
        '## 価格の話 → ## どっちを選ぶべきか3パターン → ## まとめ → ## FAQ',
    'news':
        '構成: ## ニュースの要点（3行） → ## 何が新しいのか → ## これが意味するもの → '
        '## ユーザーへの影響 → ## 競合の動き → ## 今後の予想 → ## FAQ',
    'howto':
        '構成: ## 結論：これだけやれば失敗しない → ## 必要なもの → ## 手順1〜5 → '
        '## ありがちな失敗とリカバリ → ## 関連製品 → ## FAQ',
    'guide':
        '構成: ## 結論：今ならコレを買え → ## 選ぶときの3つの観点 → '
        '## 目的別おすすめ5選（必ず製品ごとに小見出し+理由） → ## 比較表 → '
        '## まとめ → ## FAQ',
}


_BODY_PROMPT = """あなたは私のガジェットブログの執筆者です。「読者が読んで本当に役立った」と感じる記事を書いてください。

# タイトル
{title}

# 記事タイプ
{article_type}

# 検索意図（読者が求めているもの）
{search_intent}

# 構成（このタイプに最適化）
{structure}

# 過去公開記事（内部リンク素材、関連性高いもの2-3本を本文中盤〜終盤に [タイトル](URL) 形式で挿入）
{past_articles}

# 読者フィードバック（前回までの指摘、必ず反映）
{feedback}

# 必須ルール
- 文字数: 2000〜3500字（無理に長くしない）
- 結論先出し、根拠後出し
- 数値は「メーカー公式発表(2026年X月)」と出典明記。不明は「未公表」と書く
- 推測・実機所有主張は禁止（「使ってみた」は不可、「公表スペックから見ると」はOK）
- 他サイトに無い切り口を1つ含める（運用コスト、見落とされがちな欠点、想定外の使い方など）
- 冒頭に【3行まとめ】（40字×3行、AI Overviews最適化）を必ず置く
- 末尾に【FAQ】（Q&A 3つ）を必ず置く
- 「◯◯です。◯◯です。」のような単調な語尾の繰り返しを避ける

# 商品カード
- 言及した主要製品ごとに `[PRODUCT_CARD: 商品名]` を独立行で **最低2個、推奨3-5個**
- 商品名は楽天で検索ヒットする型番（メーカー+モデル名）
- 関連アクセサリ・競合・上位下位モデルでもカードを稼ぐ

# 出力
Markdown本文のみ。タイトル行は含めない。前置きや「```」は不要。

本文:
"""

_CRITIQUE_PROMPT = """以下は私のガジェットブログの記事下書きです。これを読者目線で厳しく評価してください。

# タイトル
{title}

# 本文
{body}

# 評価軸（各10点満点）
1. 結論先出しできているか（読者が最初の30秒で要点を掴めるか）
2. 他サイトに無い独自の切り口があるか（情報差別化）
3. 具体性（「便利です」のような抽象論ではなく、具体的なシーン/数値/比較が入っているか）
4. 読者の検索意図への回答度（タイトルから期待した情報が手に入るか）
5. 文章の読みやすさ（冗長な言い回しや繰り返しが無いか）

# 出力形式（JSON、コードフェンスなし）
{{"scores": {{"conclusion_first": N, "uniqueness": N, "specificity": N, "intent_match": N, "readability": N}}, "total": N, "weak_points": ["改善点1", "改善点2", ...], "rewrite_hints": "書き直し時に必ず守ること1行"}}

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


# ──────────────── タイトル選定 ────────────────

def _pick_title_with_meta(
    policy: str, existing: list[str],
    trending: list[dict], feedback: list[dict],
) -> dict:
    """タイトル + タイプ + 検索意図を JSON で返す。"""
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')

    prompt = _TITLE_PROMPT.format(
        policy=policy or '(なし)',
        trending=trend_signals.format_for_prompt(trending, limit=20),
        existing_titles='\n'.join(f'- {t}' for t in existing[-30:]) or '(なし)',
        feedback=_format_feedback(feedback),
    )

    try:
        data = _gemini_json(client, model, prompt)
    except Exception as e:
        log.warning('Title JSON parse failed, falling back: %s', e)
        return {'title': '', 'type': 'review', 'search_intent': '', 'rationale': ''}

    title = (data.get('title') or '').strip().lstrip('・-*0123456789. 　')
    return {
        'title': title[:80],
        'type': (data.get('type') or 'review').strip().lower(),
        'search_intent': (data.get('search_intent') or '').strip(),
        'rationale': (data.get('rationale') or '').strip(),
    }


# ──────────────── 本文生成 + 自己採点 + 書き直し ────────────────

def _write_body_once(
    title: str, article_type: str, search_intent: str,
    past_articles: list[dict], feedback: list[dict],
    rewrite_context: dict | None = None,
) -> str:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')

    structure = _STRUCTURE_BY_TYPE.get(article_type, _STRUCTURE_BY_TYPE['review'])

    prompt = _BODY_PROMPT.format(
        title=title,
        article_type=article_type,
        search_intent=search_intent or '(未指定)',
        structure=structure,
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


def _self_critique(title: str, body: str) -> dict:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    prompt = _CRITIQUE_PROMPT.format(title=title, body=body[:6000])
    try:
        data = _gemini_json(client, model, prompt)
        # total が無い場合 scores 平均で代用
        if 'total' not in data and 'scores' in data:
            vals = list(data['scores'].values())
            data['total'] = round(sum(vals) / max(len(vals), 1), 1)
        return data
    except Exception as e:
        log.warning('critique JSON parse failed: %s', e)
        return {'total': 10.0, 'weak_points': [], 'rewrite_hints': ''}


def _write_with_quality_gate(
    title: str, article_type: str, search_intent: str,
    past_articles: list[dict], feedback: list[dict],
    threshold: float = 7.0,
) -> tuple[str, dict]:
    """書く → 採点 → 7点未満なら1回だけ書き直し。本文と最終採点を返す。"""
    body = _write_body_once(title, article_type, search_intent, past_articles, feedback)
    log.info('Generated body v1 (%d chars)', len(body))

    critique = _self_critique(title, body)
    log.info('Self-critique v1: total=%s', critique.get('total'))

    if float(critique.get('total', 10)) >= threshold:
        return body, critique

    log.info('Below threshold (%.1f), rewriting once...', threshold)
    body2 = _write_body_once(
        title, article_type, search_intent, past_articles, feedback,
        rewrite_context=critique,
    )
    log.info('Generated body v2 (%d chars)', len(body2))

    critique2 = _self_critique(title, body2)
    log.info('Self-critique v2: total=%s', critique2.get('total'))

    # v2 のスコアが v1 より低くなければ採用、そうでなければ v1 を返す
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
    log.info(
        'Picked title=%r type=%s intent=%s rationale=%s',
        title, meta['type'], meta['search_intent'], meta['rationale'],
    )

    body, critique = _write_with_quality_gate(
        title=title,
        article_type=meta['type'],
        search_intent=meta['search_intent'],
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
