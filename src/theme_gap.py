"""ブログのキーワード分布から「手薄テーマ」をAIに提案させる。"""
import json
import re
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL


_PROMPT = """以下はブログの投稿済み記事のキーワード分析です。

ジャンル: {genre}
記事方針: {policy}
総記事数: {total}

頻出キーワード TOP30 (キーワード: 出現記事数):
{keyword_list}

このブログが**カバーできていない or 手薄なサブテーマ**を5つ提案してください。

ルール:
- ガジェット・テック領域で「書けば伸びそう・競合が薄い」ニッチを優先
- 既存頻出キーワードと隣接するが未着手の領域を重視
- 「ロングテール検索を取れそう」な切り口
- 各案は20〜40文字の具体的なテーマで

出力は厳密に下記JSONのみ:
{{"missing_themes": [
  {{"theme": "...", "reason": "なぜ伸びそうか30文字以内"}},
  ...
]}}
"""


def suggest_missing_themes(blog_genre, blog_policy, total, clusters):
    """clusters: [{keyword, count, ...}, ...]"""
    if total == 0:
        return [{
            'theme': 'まず10記事書く',
            'reason': '分析するには記事が少なすぎます',
        }]

    top = clusters[:30]
    if not top:
        return []
    kw_list = '\n'.join(f'- {c["keyword"]}: {c["count"]}記事' for c in top)

    prompt = _PROMPT.format(
        genre=(blog_genre or '指定なし')[:200],
        policy=(blog_policy or '指定なし')[:300],
        total=total,
        keyword_list=kw_list,
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5),
        )
        text = (resp.text or '').strip()
        if text.startswith('```'):
            lines = text.splitlines()
            text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])
        data = json.loads(text)
        return data.get('missing_themes', [])[:5]
    except Exception as e:
        print(f'[theme_gap] failed: {e}', flush=True)
        # 抽出を試みる
        try:
            m = re.search(r'\{[\s\S]+\}', text)
            if m:
                data = json.loads(m.group(0))
                return data.get('missing_themes', [])[:5]
        except Exception:
            pass
        return []
