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

過去に公開した記事一覧（内部リンクの素材）:
---
{past_articles}
---

# 文字数・構成
- 2500〜4000文字
- Markdown 形式（見出しは ## と ###）
- 構成（この順番を厳守）:
  1. 冒頭【3行まとめ】（各40字以内×3行。AI Overviews最適化。「## 3行まとめ」見出し）
  2. ## 結論：誰におすすめか／誰に不要か（結論先出し）
  3. ## 製品概要（型番・発売日・メーカー公表スペック）
  4. ## 詳細スペックと注目ポイント
  5. ## 想定ユースケース（朝の通勤、自宅作業など具体シナリオ）
  6. ## 競合比較（Markdown表で2機種以上、価格・主要スペック・特徴を比較）
  7. ## 注意点・落とし穴（買う前に知っておくべきこと）
  8. ## まとめ
  9. ## FAQ（よくある質問3問、Q&A形式）
  10. ## この記事の根拠（公式サイトURL・出典・スペック表参照元）

# E-E-A-T / Information Gain ルール
- 数値は出典付き。公式仕様は「メーカー公式発表（YYYY年M月）」と明記
- 不明・未公表の数値は「未公表」と書く。推測値は書かない
- **実機を所有・使用した主張は禁止**（「実際に使ってみた」「私が触った感想」等は不可）。
  代わりに「公表スペックから推測される使用感」「同シリーズの傾向から見ると」のように仕様ベースで書く
- 他サイトに無い独自の切り口を1つ必ず含める（例: 想定シーンの解像度の高さ、競合との見落とされがちな違い、購入後の運用コスト試算）
- ガジェット × 日常生活／仕事 の文脈に必ず接続する

# 内部リンク（SEO重要・必須）
- 上記「過去に公開した記事一覧」から**関連性の高い記事を2〜3本選んで内部リンク**を本文中に自然に挿入する
- 形式: 通常のMarkdownリンク `[記事タイトル](URL)` を文章の流れに溶かして配置
- 例: 「より深く知りたい人は [SONYのワイヤレスイヤホン徹底レビュー](https://...) も参考になります。」
- 関連性が低い記事を無理に紐付けるのは禁止。本当に関連する記事がなければ0〜1本でもよい
- 過去記事のタイトルとURLは**完全一致**で記載（タイトルやURLを改変しない）
- 関連記事は記事本文の中盤〜終盤に配置（冒頭や結論直後は避ける）

# 商品カード（収益化の要・必須）
- 言及した主要製品ごとに、独立した行で以下のプレースホルダを置く。**最低2個、できれば3〜5個**
- 形式: `[PRODUCT_CARD: 商品名]` （例: `[PRODUCT_CARD: EDIFIER M90]`）
- 商品名は**楽天市場で検索してヒットする一般的な型番**（メーカー名・モデル名・容量等込み）
- 置き場所: 「## H2見出し」直下、または製品に言及した段落直下の独立行（前後に空行）
- カードはコード側で楽天APIから実商品の画像・価格・Amazon/楽天ボタン付きHTMLに自動変換される
- 主題が直接買える物でなくても、関連アクセサリ・競合機種・上位下位モデルでカードを稼ぐ
- 通常のMarkdownリンク（[商品名](URL)）は使わない。カードプレースホルダのみ

# その他
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


def _format_past_articles(past: list[dict]) -> str:
    if not past:
        return '(まだ公開済み記事なし)'
    lines = []
    for a in past[:30]:
        lines.append(f"- [{a['title']}]({a['url']})")
    return '\n'.join(lines)


def _write_body(title: str, policy: str, past_articles: list[dict]) -> str:
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    prompt = _BODY_PROMPT.format(
        title=title,
        policy=policy,
        past_articles=_format_past_articles(past_articles),
    )
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

    past_articles = sheets.list_published(limit=30)
    log.info('Loaded %d past articles for internal linking', len(past_articles))
    body = _write_body(title, policy, past_articles)
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
