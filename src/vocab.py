"""ブログ設定 (genre / topic_policy / article_policy / tone) から
UI用語辞書を Gemini で自動生成する。

これにより「商品」「製品」のハードコードを廃し、
株ブログ→「銘柄」、旅行ブログ→「観光地」のように自動的にUIが適応する。
"""
import json
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL


DEFAULT_VOCAB = {
    'domain_kind': 'ブログ',
    'item_noun': '商品',
    'item_examples': ['iPhone 17 Pro', 'Pixel 10', 'Sony WH-1000XM6'],
    'compare_examples': ['iPhone 17 Pro', 'Pixel 10 Pro', 'Galaxy S26 Ultra'],
    'ranking_examples_input': ['1万円以下のワイヤレスイヤホン', 'コスパ最強の4Kモニター'],
    'topic_pick_title': 'ホットな話題から',
    'topic_pick_desc': '最新ニュースを5件提示',
    'topic_pick_title_policy': '今日のテーマから',
    'topic_pick_desc_policy': '方針に沿ってAIが自動でテーマ決定',
    'search_tile_title': '商品名を指定',
    'search_tile_desc': '特定製品のレビュー記事を作成',
    'compare_tile_title': '比較記事',
    'compare_tile_desc': '2〜5製品を横並びで比較',
    'ranking_tile_title': 'ランキング記事',
    'ranking_tile_desc': '「おすすめ◯選」型のSEO記事',
    'search_label': '商品名・キーワード',
    'compare_label': '比較する製品',
    'ranking_label': 'ランキングのテーマ',
    'topic_select_heading_news': '今ホットなトピック 5選',
    'topic_select_heading_policy': 'テーマ候補 5件',
    'feature_amazon': True,
    'feature_official_image': True,
}


_PROMPT = """以下のブログ設定から、UIに表示する用語辞書をJSONで生成してください。
このブログのジャンルに合わせて、自然で適切な日本語のラベル/プレースホルダーを返してください。

【ブログ設定】
- ジャンル: {genre}
- 記事方針: {article_policy}
- トピック方針: {topic_policy}
- 文体: {tone_prompt}

【判定ガイド】
- ガジェット → item_noun="商品", feature_amazon=true, feature_official_image=true
- 株式投資 → item_noun="銘柄", feature_amazon=false, feature_official_image=false
- 旅行 → item_noun="観光地", feature_amazon=false, feature_official_image=false
- 料理 → item_noun="レシピ" (または食材), feature_amazon=true (調理器具なら), feature_official_image=false
- 本/映画/作品 → item_noun="作品", feature_amazon=true (本ならAmazon有), feature_official_image=true (映画なら)
- 不動産 → item_noun="物件", feature_amazon=false, feature_official_image=false
- コスメ → item_noun="商品", feature_amazon=true, feature_official_image=true

【出力ルール】
- *_examples は実在のものを3〜4個、ジャンルに完全マッチするもの
- *_title は短く明確に (10文字程度)
- *_desc は20〜30文字、何ができるかを具体的に
- 出力は厳密に下記JSONのみ (前置き・コードフェンス・コメント不要):

{{
  "domain_kind": "ガジェット",
  "item_noun": "商品",
  "item_examples": ["iPhone 17 Pro", "Pixel 10"],
  "compare_examples": ["iPhone 17 Pro", "Pixel 10 Pro", "Galaxy S26"],
  "ranking_examples_input": ["1万円以下のワイヤレスイヤホン", "コスパ最強の4Kモニター"],
  "topic_pick_title": "ホットな話題から",
  "topic_pick_desc": "最新ニュースを5件提示",
  "topic_pick_title_policy": "今日のテーマから",
  "topic_pick_desc_policy": "方針に沿ってAIが自動でテーマ決定",
  "search_tile_title": "商品名を指定",
  "search_tile_desc": "特定製品のレビュー記事を作成",
  "compare_tile_title": "比較記事",
  "compare_tile_desc": "2〜5製品を横並びで比較",
  "ranking_tile_title": "ランキング記事",
  "ranking_tile_desc": "「おすすめ◯選」型のSEO記事",
  "search_label": "商品名・キーワード",
  "compare_label": "比較する製品",
  "ranking_label": "ランキングのテーマ",
  "topic_select_heading_news": "今ホットなトピック 5選",
  "topic_select_heading_policy": "テーマ候補 5件",
  "feature_amazon": true,
  "feature_official_image": true
}}
"""


def generate_vocab(blog_data):
    """ブログ設定からUI用語辞書を生成。失敗時は DEFAULT_VOCAB を返す。"""
    genre = (blog_data.get('genre') or '').strip()
    article_policy = (blog_data.get('article_policy') or '').strip()
    topic_policy = (blog_data.get('topic_policy') or '').strip()
    tone_prompt = (blog_data.get('tone_prompt') or '').strip()

    if not (genre or article_policy or topic_policy):
        return DEFAULT_VOCAB.copy()

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_PROMPT.format(
                genre=genre or '(未設定)',
                article_policy=article_policy or '(未設定)',
                topic_policy=topic_policy or '(未設定)',
                tone_prompt=tone_prompt or '(未設定)',
            ),
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or '').strip()
        if text.startswith('```'):
            lines = text.splitlines()
            text = '\n'.join(lines[1:-1] if lines and lines[-1].strip() == '```' else lines[1:])
        data = json.loads(text)
        print(f'[vocab] generated for "{genre}": item_noun={data.get("item_noun")} domain={data.get("domain_kind")}', flush=True)
    except Exception as e:
        print(f'[vocab] gemini failed, using default: {e}', flush=True)
        return DEFAULT_VOCAB.copy()

    out = DEFAULT_VOCAB.copy()
    for k, v in data.items():
        if v is not None:
            out[k] = v
    # 型の防御
    for list_key in ('item_examples', 'compare_examples', 'ranking_examples_input'):
        if not isinstance(out.get(list_key), list) or not out[list_key]:
            out[list_key] = DEFAULT_VOCAB[list_key]
    for bool_key in ('feature_amazon', 'feature_official_image'):
        out[bool_key] = bool(out.get(bool_key))
    return out


def merge_with_default(vocab):
    """vocabが部分的でも欠けたキーをデフォルトで埋める。"""
    out = DEFAULT_VOCAB.copy()
    if isinstance(vocab, dict):
        for k, v in vocab.items():
            if v is not None:
                out[k] = v
    return out
