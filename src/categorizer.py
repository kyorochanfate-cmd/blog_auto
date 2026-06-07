"""Gemini で記事タイトル/本文から はてなブログ用カテゴリを推測する。

はてなブログでは記事ごとに複数カテゴリ (= タグ) を AtomPub で付与可能。
カテゴリは検索流入・グループ新着・タグ一覧などの導線になるので、できる限り
具体的な固有名詞や検索キーワードを当てるのが効く。
"""
import json
import re
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL


_PROMPT = """あなたは経験豊富なブログ編集者です。
以下の記事タイトルと本文冒頭をもとに、**はてなブログ用のカテゴリ(タグ)を3〜5個** 提案してください。

【記事タイトル】
{title}

【本文(冒頭1000文字)】
{body_excerpt}

【良いカテゴリの条件】
- 検索されやすい固有名詞(製品名・メーカー名・サービス名)を優先: 「iPhone」「Sony」「ChatGPT」など
- 同じジャンルの記事を集める意味で機能する一般カテゴリも1つ: 「ガジェット」「ニュース」「レビュー」「セール情報」など
- 1個1〜12文字
- 抽象すぎるもの(例: 「考察」「日記」「メモ」)、汎用すぎるもの(例: 「テクノロジー」一語のみ)は避ける
- 重複する内容は避ける(「iPhone」と「アイフォン」のような)

【出力形式 (厳守・前置きやコードフェンス禁止)】
{{"categories": ["カテゴリ1", "カテゴリ2", "カテゴリ3", ...]}}
"""


def suggest_categories(title, body, max_count=5):
    """Gemini に記事カテゴリを推測させる。

    Args:
        title: 記事タイトル
        body: 記事本文 (Markdown)
        max_count: 最大カテゴリ数 (デフォルト5)

    Returns:
        list[str]: カテゴリ名のリスト。失敗時は []。
    """
    if not title:
        return []

    body_excerpt = (body or '')[:1000]
    prompt = _PROMPT.format(title=title.strip(), body_excerpt=body_excerpt)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4),
        )
        text = (resp.text or '').strip()
    except Exception as e:
        print(f'[categorizer] gemini failed: {e}', flush=True)
        return []

    # コードフェンス除去
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON 抽出を試行
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            print(f'[categorizer] no JSON in response: {text[:200]}', flush=True)
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []

    raw = data.get('categories') or []
    out = []
    seen = set()
    for c in raw:
        c = (c or '').strip()
        # ハッシュ記号や余計な装飾を除去
        c = c.lstrip('#').strip()
        if not c or len(c) > 20:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= max_count:
            break

    return out
