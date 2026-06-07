import json
import random
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

# 東証の業種別証券コード帯
_TSE_SECTORS = [
    ('水産・農林・鉱業・建設', 1300, 1999),
    ('食料品・繊維・パルプ・紙', 2000, 2999),
    ('化学・石油・ゴム・窯業・土石', 3000, 3999),
    ('医薬品・精密機器・その他製造', 4000, 4999),
    ('鉄鋼・非鉄金属・金属製品・機械', 5000, 5999),
    ('電気機器・輸送用機器', 6000, 6999),
    ('卸売・小売', 7000, 7999),
    ('銀行・証券・保険・不動産', 8000, 8999),
    ('陸運・海運・空運・倉庫・情報通信・サービス', 9000, 9999),
]


def _random_stock_hint() -> str:
    """Python側でランダムにセクター・コード帯を決め、Geminiへのヒントを生成する。"""
    sector_name, code_min, code_max = random.choice(_TSE_SECTORS)
    target = random.randint(code_min, code_max)
    return (
        f'【今回のランダム選定条件 (必ず従うこと)】\n'
        f'- セクター: {sector_name}\n'
        f'- 証券コードの目安: {target} 付近 ({code_min}〜{code_max} の範囲)\n'
        f'- 大型株・超有名企業（トヨタ・ソニー・ソフトバンク・任天堂など）は除外する\n'
        f'- 時価総額が小さめのマイナー・中小型銘柄を積極的に選ぶ\n'
        f'- 一般的に知名度が低く、このブログで初めて知る読者が多そうな企業を優先する\n'
    )


_DEFAULT_CRITERIA = """選定基準:
- 同じテーマ・トピックに言及する記事が複数あるもの (話題性が高い)
- 単発でも注目度が高そうなもの
- 重複・類似テーマは1つにまとめる"""

_JSON_SCHEMA = """{
  "topics": [
    {
      "name": "トピック名 (簡潔に40文字以内)",
      "summary": "1-2文の要約 (100文字以内)",
      "indices": [関連する記事のインデックス番号(整数)のリスト]
    }
  ]
}"""


_AR_VR_NICHE_EMPHASIS = """
【★★★★★ このブログは「AR/XR グラス・空間コンピューティング専門ブログ」(絶対遵守) ★★★★★】

これは雑食ガジェットブログでも一般テクノロジーニュースサイトでもない。
**「AR/XR グラス・VR ヘッドセット・空間コンピューティング・スマートグラス」を中心に、
たまに面白い新興ハードウェア (Kickstarter 系・専門用途・ニッチ消費者向け) を扱う特化ブログ**。

【絶対除外する題材 (大手メディアに勝てない・読者層と合わない)】
❌ AI 政策・規制・法律 (例: AI 生成画像の表示義務化、独禁法、データ保護)
❌ 学術研究・公的機関ニュース (例: 文化財研究所、大学発表、官庁発表)
❌ BtoB / 業界 DX ニュース (例: 不動産テック、建設DX、医療DX、行政DX)
❌ AI モデル発表・LLM 比較・生成AI サービスニュース (Gemini/ChatGPT/Claude モデル名のみで判断するもの)
❌ 経営・経済・株価・決算ニュース (経団連、業界団体、企業合併、IPO)
❌ 一般 IT ニュース (クラウド、サーバー、データセンター、セキュリティ事件)
❌ 期間限定セール・タイムセール・キャンペーン

【採用する題材 (3カテゴリのみ・これ以外は絶対選ばない)】

🟢 カテゴリA: AR/XR グラス・VR・空間コンピューティング (記事全体の 70%・コア)
- **AR/XR グラス**: XREAL / VITURE / Rokid / Meta Orion / Google Project Aura / Apple Vision / Snap Spectacles / RayNeo / TCL RayNeo / Brilliant Labs Frame / INMO 等
- **VR ヘッドセット**: Meta Quest 3/4 / Apple Vision Pro / Sony PSVR2 / Pico 4 / Bigscreen Beyond / Pimax 等
- **空間コンピューティング**: visionOS / Android XR / Meta Horizon OS / Snapdragon AR2 Gen2 / spatial computing 概念解説
- **AR/VR 周辺技術**: マイクロLED / シリコン光学 / ハンドトラッキング / アイトラッキング / フォービエーテッドレンダリング / passthrough / Pancake レンズ / Birdbath
- **AR/VR アプリ・コンテンツ**: 仮想モニター系 (Immersed / Virtual Desktop)、AR オフィス、3D 設計、VR ゲーム
- **大手の参入動向**: Google / Apple / Meta / Microsoft / Sony / Samsung の AR/VR 関連発表

🟢 カテゴリB: 面白い新興ガジェット (記事全体の 20%・SEO 競合少ない物理デバイス限定)
判断: 「Google で日本語の詳しい記事が3つ以下」かつ「物理的に買える消費者向け製品」
- 海外スタートアップ・Kickstarter / Indiegogo の革新ガジェット
- 特殊用途の物理デバイス (例: ARコンタクトレンズ、BCI民生機、触覚デバイス、新型コントローラ)
- e-paper / メカニカルキーボード / フィルムカメラ復興 / レトロガジェット の最新動向 (reMarkable、BOOX、HHKB、Leica 等)
- 中国・台湾・韓国メーカーのニッチハードウェア (大手未紹介の型番)

🟡 カテゴリC: 隣接ガジェット (記事全体の 10%・AR/VR との接続が明確な時のみ)
- AR グラスを母艦として使うスマートフォン (Pixel 9 Pro 等の文脈で語れる時だけ)
- AR/VR と統合される健康センサー系ウェアラブル (Apple Watch + Vision Pro 等)
- 空間コンピューティングの代替として比較できるノートPC・タブレット

🔴 絶対に選ばないトピック (大手メディアに勝てない・読者層と合わない)
- 一般 AI/LLM ニュース (Gemini/ChatGPT のモデル発表、AI規制、AI政策、AI 倫理)
- 学術・公的機関のニュース (大学発表、研究機関、文化財、官庁)
- BtoB / DX ニュース (不動産テック、建設DX、医療DX、自治体DX)
- 経営・経済 (決算、M&A、IPO、株価、経団連、業界団体)
- 一般 IT ニュース (クラウド、データセンター、セキュリティ事件、サイバー攻撃)
- スマホ単体レビュー、PC 単体レビュー、Anker 系雑記 (大手が既に大量)
- セール情報・タイムセール・キャンペーン

判断ロジック:
1. **「これは AR/XR グラス・VR・空間コンピューティングに直結するか?」** YES なら採用 (カテゴリA)
2. NO なら → **「物理的に消費者が買えるニッチハードウェアか?」** YES なら採用 (カテゴリB)
3. NO なら → **「カテゴリA との明確な接続が記事内で語れるか?」** YES なら採用 (カテゴリC、10%枠まで)
4. どれにも該当しなければ **採用しない** (1件もトピック取れなくても OK、雑食記事を書くより無記事の方がマシ)

"""


def _build_prompt_header(topic_policy: str, genre: str = '', niche_focus: str = '') -> str:
    if topic_policy and topic_policy.strip():
        criteria = f'【トピック選定方針 (最優先で従うこと)】\n{topic_policy.strip()}'
    else:
        # niche_focus が設定されてれば、専門ブログとして強くバイアスする
        nf = (niche_focus or '').lower()
        if 'ar' in nf or 'vr' in nf or '空間' in nf or 'スマートグラス' in nf or 'spatial' in nf:
            genre = (genre or '').strip() or 'AR/VR・空間コンピューティング'
            criteria = (
                f'【★最優先: このブログは「{niche_focus}」専門ブログ★】\n'
                + _AR_VR_NICHE_EMPHASIS
                + '\n' + _DEFAULT_CRITERIA
            )
            return (
                '以下は日本国内および海外の最近のニュース記事リストです (英語記事と日本語記事が混在することがあります)。\n'
                'これらの中から **AR/VR・空間コンピューティング領域に直結するトピック** をクラスタリングしてください。\n'
                '同じ事案を扱う日英記事は同じトピックにまとめてください。\n'
                'トピック名は日本語で出力してください。\n\n'
                f'{criteria}\n\n'
                '出力は厳密に下記のJSON形式のみ (前置き・コードフェンス・コメント等は一切不要):\n'
                f'{_JSON_SCHEMA}\n\n'
                '【記事リスト】\n'
            )
        genre = (genre or '').strip()
        if genre:
            # 「ガジェット」を含む genre は特別扱い: 物理的な製品を強く優先
            is_gadget = any(g in genre.lower() for g in ('ガジェット', 'gadget', 'デバイス', 'device'))
            gadget_emphasis = ''
            if is_gadget:
                gadget_emphasis = (
                    '\n【★★★ ガジェットブログとして最優先で守るルール ★★★】\n'
                    '読者は「物理的なガジェット」の情報を求めて来ている。以下の優先順位で選定:\n'
                    '\n'
                    '🟢 強く優先するトピック (物理的な製品・ハードウェア):\n'
                    '- スマホ、タブレット、ノートPC、デスクトップPC、自作PCパーツ\n'
                    '- イヤホン、ヘッドホン、スマートウォッチ、スマートグラス\n'
                    '- カメラ、レンズ、ドローン\n'
                    '- ゲーム機(Switch, PS等)、コントローラ、ヘッドセット\n'
                    '- スマート家電、IoT、ロボット掃除機\n'
                    '- 充電器、モバイルバッテリー、ケーブル、ストレージ\n'
                    '- 電動工具、便利グッズ、変わり種ガジェット(冷却ベスト、電動爪切り等)\n'
                    '- カーガジェット(ナビ、ドラレコ、EV関連)\n'
                    '\n'
                    '🔴 弱く扱う、または除外するトピック:\n'
                    '- 企業の決算・株価・M&A・経営戦略 (純粋な業界ビジネスニュースは別ブログ向け)\n'
                    '- 政策・規制・ガバナンス・法律 (例: AIガバナンス、データ法、独禁法等)\n'
                    '- フィッシング詐欺・セキュリティ警告 (実害なら可だが優先度低)\n'
                    '- ソフトウェア・SaaS・Webサービスのみの話題 (物理製品が絡まない場合)\n'
                    '- 暗号資産・FinTech・金融サービス\n'
                    '- メタバース・サービス終了等の運営関連\n'
                    '\n'
                    '🟡 グレーゾーン (ガジェット要素があれば可、なければ除外):\n'
                    '- AI機能 → 「ガジェットに搭載される具体AI機能」なら可、抽象的なAI論はNG\n'
                    '- 通信事業者 → 「具体的なルーター/Wi-Fi製品/プラン」なら可、決算はNG\n'
                    '\n'
                    '**8割以上を 🟢 から選ぶこと**。残り 2割でも 🔴 は避ける。\n'
                    '\n'
                    '【★★ さらに「ワクワク度」で重みづけする ★★】\n'
                    'このブログは「読者がワクワクする最先端テクノロジー&ガジェット」をテーマにする。\n'
                    'スペック比較や値上げニュースだけで終わらせず、以下の「ワクワク要素」が強いトピックを優先する:\n'
                    '\n'
                    '✨ 強くワクワクするトピック (積極的に選ぶ):\n'
                    '- 具体的な実演・デモ動画あり (例: 「Boston Dynamics Atlas が冷蔵庫を運搬」「人型ロボがバク転」)\n'
                    '- 「初めて○○を実現」「世界初」「○年で実用化」など breakthrough 要素\n'
                    '- フロンティア技術 (人型ロボ、ARグラス、BCI、量子、バイオミメティクス、生体センサー)\n'
                    '- 未来予測の手がかり (「これが標準になる日」を感じさせる発表)\n'
                    '- 物理世界に作用するAI (具現化されたAI、デジタルではない動くもの)\n'
                    '- 変わった発想のガジェット (冷却ベスト、電動爪切り、視線追跡デバイス等)\n'
                    '- 「今までできなかったことができる」型の革新\n'
                    '\n'
                    '😴 ワクワクしないトピック (避ける):\n'
                    '- 「○○が値上げ」「○○のセール情報」のような価格動向のみ\n'
                    '- 単なる決算・経営情報・株価動向\n'
                    '- 既存製品のマイナーアップデート (ファームウェア更新、軽微なバグ修正のみ)\n'
                    '- 「○○を発表」だけで具体的な実演・新機能の中身がないもの\n'
                    '- 訴訟・法律・規制ニュース(技術的興奮を呼ばない)\n'
                    '\n'
                    '判断軸: 「読者が記事タイトルを見て、思わず開いて中身を見たくなるか?」\n'
                    'Atlas が冷蔵庫を運ぶ動画のような「目撃したくなる」「未来の予兆を感じる」トピックを優先せよ。\n'
                    '\n'
                    '【★★ さらに「半年後も読まれる資産性」で重みづけ ★★】\n'
                    'このブログは中長期SEO目標。1日1本だけ書くので、「3日後に古びるネタ」は致命的。\n'
                    '\n'
                    '🟢 半年後も検索流入が見込めるトピック (優先):\n'
                    '- 新カテゴリの登場 (例: 「人型ロボット家庭用」「BCI 民生機」) — 数ヶ月〜数年後も学びに来る人いる\n'
                    '- 業界の方向転換・パラダイムシフト (「○○の終わり」「△△の始まり」)\n'
                    '- 技術用語・概念の登場 (例: 「Matter 2.0」「PD 3.1」「NPU」) — 検索の長期需要あり\n'
                    '- 製品カテゴリの代表機種が登場 (「○○の決定版」になりうるもの)\n'
                    '- 議論を呼ぶ動き (賛否分かれる動きは長く検索される)\n'
                    '\n'
                    '🔴 賞味期限の短すぎるトピック (避ける):\n'
                    '- ○月○日限定セール、Amazonタイムセール、ポイント還元キャンペーン\n'
                    '- 「○○が話題」「○○がバズった」だけの瞬間ネタ\n'
                    '- 旧モデルの軽微なファームウェアアップデート\n'
                    '- 「○○年版」が古くなる程度の微妙な情報 (週単位で古びるネタ)\n'
                    '\n'
                    '⚠️ 鮮度ネタでも資産化できる切り口があるなら拾う:\n'
                    '- 値上げニュース → 「ゲーム機の価格戦略の歴史と Switch 2 」のように翻訳\n'
                    '- 新製品発表 → 「このカテゴリの選び方の決定版」として書き直し\n'
                    '\n'
                    '判断: 「6ヶ月後に Google 検索でこのキーワード/題材を打ち込む人がいるか?」を毎回問う。\n'
                )

            genre_focus = (
                f'【★最優先: ジャンル「{genre}」に強く関連するトピックのみ選ぶ★】\n'
                f'- このブログは「{genre}」専門ブログ。読者は「{genre}」の情報を求めて来る。\n'
                f'- 「{genre}」と直接関係しないトピック（一般経済・政治・芸能・スポーツ・天気・地域ニュース等）は選ばない。\n'
                f'- 関連性が弱い・周辺的なものより、コアに「{genre}」を扱うものを優先。\n'
                f'- 多様性より関連性を優先。全件「{genre}」に寄っていてOK。\n'
                + gadget_emphasis +
                '\n'
            )
            criteria = genre_focus + _DEFAULT_CRITERIA
        else:
            criteria = _DEFAULT_CRITERIA
    return (
        '以下は日本国内および海外の最近のニュース記事リストです (英語記事と日本語記事が混在することがあります)。\n'
        'これらの中からトピックを選び、関連する記事をクラスタリングしてください。\n'
        '同じ事案を扱う日英記事は同じトピックにまとめてください (例: Apple の発表を The Verge と ITmedia の両方が報じていれば1トピック)。\n'
        'トピック名は日本語で出力してください (英語ソースのみの場合でも日本語化する)。\n\n'
        f'{criteria}\n\n'
        '出力は厳密に下記のJSON形式のみ (前置き・コードフェンス・コメント等は一切不要):\n'
        f'{_JSON_SCHEMA}\n\n'
        '【記事リスト】\n'
    )


def select_top_topics(items, count=5, exclude_topic_names=None, topic_policy='', genre='', niche_focus=''):
    listing = '\n'.join(
        f'[{i}] {item["title"]}\n    要約: {_clip(item["summary"], 200)}\n    出典: {item["source"]}'
        for i, item in enumerate(items)
    )

    exclude_section = ''
    exclude_names = [n for n in (exclude_topic_names or []) if n]
    if exclude_names:
        # 多すぎると Gemini が拾い切れないので最新60件まで
        exclude_list = '\n'.join(f'- {n}' for n in exclude_names[:60])
        exclude_section = (
            '\n\n【★★★ 過去30日間に投稿済みの記事タイトル一覧 — 類似は絶対に避ける ★★★】\n'
            f'{exclude_list}\n\n'
            '【厳格な禁止事項】\n'
            '- 上記と **同じ製品・同じシリーズ・同じイベント・同じ話題** は絶対に選ばない\n'
            '- 上記と **同じキーワード**(製品名、メーカー名、技術名)が中心テーマになる記事は選ばない\n'
            '  例: 過去に「Switch 2 値上げ」を書いていたら、Switch 2 関連は全て避ける(値上げに限らず)\n'
            '  例: 過去に「Anker 新型バッテリー」を書いていたら、Anker のバッテリー製品は全て避ける\n'
            '  例: 過去に「Atlas ロボット冷蔵庫」を書いていたら、Boston Dynamics 関連は全て避ける\n'
            '- 上記と **同じジャンルの似たり寄ったりな話題** は避ける\n'
            '  例: 既にARグラス記事3本あるなら、新たなARグラス記事は選ばない(他のジャンルを優先)\n'
            '\n'
            '【代わりに探すべき方向性】\n'
            '- 上記リストにまだ登場していない **メーカー・技術カテゴリ・用途領域** を優先\n'
            '- リストの偏り(例: スマホ系が多すぎる)を見て、不足しているジャンルを意識的に選ぶ\n'
            '- 全く新しい角度・予想外の組み合わせ・マイナーだが面白い動きを発掘する\n'
            '\n'
            '読者は「またこの話題か」と感じたら離脱する。多様性は最優先事項。\n'
        )

    prompt = (
        _build_prompt_header(topic_policy, genre, niche_focus)
        + listing
        + exclude_section
        + f'\n\n上記から {count} 件選んでください。'
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type='application/json',
            temperature=0.4,
        ),
    )
    data = json.loads(response.text)

    topics = []
    for t in data.get('topics', [])[:count]:
        related = [items[i] for i in t.get('indices', []) if 0 <= i < len(items)]
        if related:
            topics.append({
                'name': t.get('name', '(無題)'),
                'summary': t.get('summary', ''),
                'related': related,
            })
    return topics


def generate_policy_topics(topic_policy, count=5, exclude_topic_names=None):
    """ニュース不要。topic_policy だけを元に Gemini がトピックを生成する。"""
    exclude_section = ''
    exclude_names = [n for n in (exclude_topic_names or []) if n]
    if exclude_names:
        exclude_list = '\n'.join(f'- {n}' for n in exclude_names)
        exclude_section = (
            '\n\n【既に紹介済み (これらと同一・類似は絶対に選ばない)】\n'
            f'{exclude_list}\n\n'
            '上記と同じ銘柄・テーマは除外し、必ず別のものを選んでください。'
        )

    random_hint = _random_stock_hint() if 'ランダム' in topic_policy else ''

    prompt = (
        '以下の方針に従い、今日取り上げるトピックを選定してください。\n'
        'ニュース記事には依存せず、方針に基づいて独自にトピックを決めてください。\n\n'
        f'【方針】\n{topic_policy.strip()}\n\n'
        + (f'{random_hint}\n' if random_hint else '')
        + f'{exclude_section}\n\n'
        f'{count} 件のトピックを生成してください。\n'
        '出力は厳密に下記のJSON形式のみ (前置き・コードフェンス・コメント等は一切不要):\n'
        '{\n'
        '  "topics": [\n'
        '    {\n'
        '      "name": "トピック名 (簡潔に40文字以内)",\n'
        '      "summary": "1-2文の要約 (100文字以内)",\n'
        '      "indices": []\n'
        '    }\n'
        '  ]\n'
        '}'
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.7),
    )

    text = (response.text or '').strip()
    # コードフェンスを除去
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])

    data = json.loads(text)

    return [
        {
            'name': t.get('name', '(無題)'),
            'summary': t.get('summary', ''),
            'related': [],
        }
        for t in data.get('topics', [])[:count]
        if t.get('name')
    ]


def _clip(s, n):
    s = (s or '').replace('\n', ' ').strip()
    return s[:n] + ('…' if len(s) > n else '')
