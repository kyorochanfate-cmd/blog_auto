"""B: ①現状データ → Gemini で読者傾向分析 → ②新テイスト方針指示書 を上書き。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

from google import genai

from . import sheets

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_PROMPT = """あなたは私のはてなブログのエディターです。
以下は直近のGA4アクセスデータ（CSV風）です。読者傾向を分析し、明日の方針を立ててください。

データ列: ts, date, page_path, page_title, views, avg_engagement_sec, gemini_summary
データ:
{rows}

過去の方針（参考、空の場合あり）:
{previous_policy}

以下の形式で**そのまま出力**してください。前置きや「```」などは不要。

更新日時: {now}
注目テーマ:
  - （箇条書き1）
  - （箇条書き2）
  - （箇条書き3）
タイトル方針: （1行）
本文の長さ・テイスト: （1行）
避けるべき要素: （1行）
明日書くべき記事候補（タイトル案を5本、ガジェット関連に絞る）:
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

    prompt = _PROMPT.format(
        rows=_format_rows(rows),
        previous_policy=previous or '(なし)',
        now=now,
    )

    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    log.info('Calling Gemini for trend analysis (model=%s, rows=%d)', model, len(rows))
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (resp.text or '').strip()
    if not text:
        raise RuntimeError('Gemini returned empty analysis')

    sheets.write_policy(text)
    log.info('Policy updated (%d chars)', len(text))
    return text
