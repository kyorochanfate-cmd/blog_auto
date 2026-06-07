"""ロングテールキーワード提案。"""
import json
import re
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL


_PROMPT = """ガジェットブロガー向けに、以下のテーマからロングテールキーワード(検索ボリュームは中程度だが大手と競合しないニッチな切り口)を5つ提案してください。

元のテーマ: {theme}

ルール:
- 「製品名+特定の用途」「製品名+特定の数値・期間」「製品名 vs 別製品」「対象読者別」「具体的な悩み」のような切り口
- ありふれた「{theme} レビュー」「{theme} まとめ」のような切り口は避ける
- 検索する人の具体的な悩みに刺さる表現
- 各案は記事タイトルではなく「テーマ・切り口」を返す (短めに)

出力は厳密に下記JSONのみ (前置き・コードフェンス不要):
{{"suggestions": [
  {{"theme": "ニッチなテーマ案", "reason": "なぜこれが良いか30文字以内"}},
  ...
]}}
"""


def suggest_longtail(theme):
    if not theme or not theme.strip():
        return []
    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_PROMPT.format(theme=theme.strip()),
            config=types.GenerateContentConfig(temperature=0.7),
        )
    except Exception as e:
        print(f'[longtail] gemini failed: {e}', flush=True)
        return []
    text = (resp.text or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])
    try:
        data = json.loads(text)
    except Exception:
        # JSON抽出を試行
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    return [s for s in data.get('suggestions', [])[:5] if s.get('theme')]
