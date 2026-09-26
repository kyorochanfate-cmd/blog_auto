"""B: ①現状データ → Gemini で読者傾向分析 → ②新テイスト方針指示書 を上書き。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

from google import genai

from . import search_console, sheets

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_PROMPT = """あなたは私のはてなブログのエディターです。
直近のGA4アクセスデータと Google Search Console の検索クエリを読んで、
読者傾向を分析し、明日の方針を立ててください。

# GA4: ページ別アクセス
データ列: date | title | path | views | engagement(sec)
{rows}

# Search Console: 検索クエリ（直近28日、clicks降順）
{gsc_queries}

# 過去の方針（参考、空の場合あり）
{previous_policy}

# 分析の観点
- GSCクエリは「読者が実際にGoogleで検索した語」。狙い目の検索意図を特定する重要シグナル
- impressions が多いのに clicks が少ない → タイトル改善余地（CTR低い）
- position が10〜20位 → 順位を上げれば伸びやすい狙い目
- GA4とGSCの両方で人気のテーマは強化、片方だけのテーマは原因を考える
- ガジェット × 仕事効率／日常生活 の文脈に必ず接続する

# 出力形式（そのまま出力。前置きや「```」は不要）

更新日時: {now}
注目テーマ:
  - （GSC/GA4から読み取れる狙い目テーマ1）
  - （狙い目テーマ2）
  - （狙い目テーマ3）
CTR改善対象（impressionsあるのにclicks少ないクエリ、最大3つ）:
  - 「クエリ」: 改善案
タイトル方針: （1行）
本文の長さ・テイスト: （1行）
避けるべき要素: （1行）
明日書くべき記事候補（タイトル案を5本、ガジェット関連、GSCクエリに紐づく形で）:
  1. ...
  2. ...
  3. ...
  4. ...
  5. ...
"""


def _format_rows(rows: list[list]) -> str:
    lines = []
    for r in rows:
        padded = r + [''] * (7 - len(r))
        ts, date, path, title, views, eng, _summary = padded[:7]
        lines.append(f"{date} | {title[:40]} | {path} | views={views} | eng={eng}s")
    return '\n'.join(lines)


def run() -> str:
    """①を読んでGemini分析→②に書き込み。書き込んだ本文を返す。"""
    sheets.ensure_headers()
    rows = sheets.read_current_recent(limit=200)
    if not rows:
        log.warning('①現状データが空。分析スキップ。')
        return ''

    previous = sheets.read_policy()
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M')

    gsc_queries = search_console.fetch_top_queries(limit=50)
    gsc_text = search_console.format_for_prompt(gsc_queries, top=30)

    prompt = _PROMPT.format(
        rows=_format_rows(rows),
        gsc_queries=gsc_text,
        previous_policy=previous or '(なし)',
        now=now,
    )

    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    log.info('Calling Gemini for trend analysis (model=%s, rows=%d)', model, len(rows))
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (resp.text or '').strip()
    if not text:
        raise RuntimeError('Gemini returned empty analysis')

    sheets.write_policy(text)
    log.info('Policy updated (%d chars)', len(text))
    return text
