"""C: ②方針 → Gemini で記事執筆 → ③投稿待ち記事 に追加。

1日1本だけ追加。直近の同名タイトルがあれば別候補を選ぶ。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

from google import genai

from . import sheets

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_TITLE_PROMPT = """以下は、私のガジェットブログの「明日書くべき記事候補」を含む方針指示書です。
方針:
---
{policy}
---

直近で既に書いた記事タイトル（重複させない）:
{existing}

「明日書くべき記事候補」5本の中から、まだ書いていない、最も価値が高いタイトルを1本だけ選び、
**タイトル文字列のみ**を1行で出力してください。前置き・記号・引用符は不要。
"""

_BODY_PROMPT = """あなたは私のガジェットブログの執筆者です。以下のタイトルでブログ記事1本を執筆してください。

タイトル: {title}

執筆方針（方針指示書から抜粋）:
---
{policy}
---

要件:
- 2500〜4000文字
- Markdown 形式（見出しは ## と ###）
- 結論を最初に、根拠を後段に
- 推測ではなく仕様・公式情報を優先（ガジェットの深い事実分析を重視）
- 推測や根拠不明な数字は書かない。不明なら「未公表」と書く
- 推薦製品があれば、本文末尾に Amazon検索リンク
  例: [Amazonで検索](https://www.amazon.co.jp/s?k=製品名)
- 出力は**Markdown本文のみ**。タイトル行は含めない。前置きや「```」は不要

本文:
"""


def _pick_title(policy: str, existing: list[str]) -> str:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    existing_text = '\n'.join(f'- {t}' for t in existing[-30:]) or '(なし)'
    prompt = _TITLE_PROMPT.format(policy=policy, existing=existing_text)
    resp = client.models.generate_content(model=model, contents=prompt)
    title = (resp.text or '').strip().splitlines()[0].strip()
    title = title.lstrip('・-*0123456789. 　')
    if not title:
        raise RuntimeError('Gemini returned empty title')
    return title[:80]


def _write_body(title: str, policy: str) -> str:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    prompt = _BODY_PROMPT.format(title=title, policy=policy)
    resp = client.models.generate_content(model=model, contents=prompt)
    body = (resp.text or '').strip()
    if body.startswith('```'):
        lines = body.splitlines()
        body = '\n'.join(lines[1:-1] if lines[-1].startswith('```') else lines[1:])
    if len(body) < 500:
        raise RuntimeError(f'Generated body too short ({len(body)} chars)')
    return body


def run() -> str | None:
    """方針を読み記事1本を生成→③に追記。書いたタイトルを返す。"""
    sheets.ensure_headers()
    policy = sheets.read_policy()
    if not policy.strip():
        log.warning('②方針が空。執筆スキップ（先に --analyze を回す必要あり）。')
        return None

    existing = sheets.list_queue_titles()
    title = _pick_title(policy, existing)
    log.info('Picked title: %s', title)

    if title in existing:
        log.warning('Title already in queue: %s. Skipping.', title)
        return None

    body = _write_body(title, policy)
    log.info('Generated body (%d chars)', len(body))

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
