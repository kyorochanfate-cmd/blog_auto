import json
import re
import urllib.parse
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL


# ===== 記事タイプ別の構成テンプレート =====
# 毎記事「いつも同じ形」になるのを防ぐため、トピックの性質に応じて
# Gemini が記事タイプを判定 → そのタイプ専用の構成指示を本文プロンプトに注入する。

_TYPE_INSTRUCTIONS = {
    'A': """【★この記事の構成タイプ: 速報型★】
ニュース速報として簡潔に伝える構成。全体 **2000〜2800字** (短め)。装飾控えめ、スピード感重視、期待度スコアセクションは作らない。

【★見出しは固定文言ではなく、トピックの中身に合わせて毎回考えること★】
以下の「狙い」を満たすセクションを5〜6個並べる(順序は最適化してOK)。
**「## 何が起きたか」「## 今後どうなるか」のような汎用ラベルは禁止**。製品名・固有名詞を含めた具体的な見出しにする。

1. **ニュースの要旨を伝える章**
   - 狙い: 何のニュースか3行で分かるように
   - 見出し例(これらは例。トピックに応じて変える):
     - 「Anker、新型モバイルバッテリーを国内発売」
     - 「ソニー REON POCKET 6 がついに登場」
     - 「Switch 2 の値上げが正式発表」
2. **押さえるべきポイントの章**
   - 狙い: 3つの注目点を箇条書きで
   - 見出し例: 「ここが進化した3つの点」「先代との違いはこの3つ」「特に注目したい新機能」
3. **背景・意義の章**
   - 狙い: なぜ今このタイミングか、業界文脈
   - 見出し例: 「ライバル製品が控える中での発表」「価格戦略の意図はどこに」
4. **見通しの章**
   - 狙い: 今後の展開を短く
   - 見出し例: 「日本での予約はいつから」「販売数はどこまで伸びるか」
5. ## まとめ (これは固定でOK)
6. ## よくある質問 (FAQ 3問、固定でOK)""",

    'B': """【★この記事の構成タイプ: 深掘り解説型★】
個人ブロガーが時間かけて書く濃い記事。全体 **3500〜5500字** (厚め)。

【★見出しは固定文言ではなく、トピックの中身に合わせて毎回考えること★】
以下の狙いを満たすセクションを構成する。**「## 私が特に気になっているポイント」「## 今後の見通し」のような汎用ラベルは避け**、製品名・具体内容を含む見出しにする。

1. (製品レビュー型のみ) **## 私の期待度スコア** ← これは固定見出しでOK
   - `期待度: ★★★★☆ (4/5)` `コスパ予想: ★★★☆☆ (3/5)` `購入欲: ★★★★★ (5/5)`
   - その後 2〜3行で理由
2. **製品/トピックの基本情報を解説する章** (H2 数個)
   - 見出し例: 「Sony WH-1000XM7 の主なスペック」「ノイキャン性能はどう進化した」など、製品名+具体内容
3. **個人的に気になる注目点の章**
   - 狙い: 3つを番号付きで、各80〜150字
   - 見出し例: 「個人的に注目したい3つの進化点」「ここがワクワクするポイント」「実機で確かめたい3つ」
4. **批判視点の章**
   - 狙い: 微妙な点を2〜3個
   - 見出し例: 「価格設定に感じる違和感」「ここはちょっと残念」「同価格帯と比べたら見劣りする部分」
5. **未来予測の章**
   - 狙い: 1〜2年後の展開を200〜400字
   - 見出し例: 「来年このシリーズはどうなる」「業界全体に与える影響」
6. **競合・業界影響の章**
   - 見出し例: 「Apple・Galaxy陣営はどう対応するか」「他社製品との位置づけ」
7. (製品の場合) **対象読者の章** / (ニュースの場合) **影響を受ける人の章**
   - 見出し例: 「こんな人には強くおすすめ」「投資家が注目すべき理由」
8. ## まとめ (固定OK)
9. ## よくある質問 (固定OK)""",

    'C': """【★この記事の構成タイプ: 体験予想型★】
個人的に気になる物について雑談調で書く。全体 **2500〜3500字**。カジュアル、ですます調くだけ気味、独白に近い。「私は」「個人的には」「正直」「ぶっちゃけ」など主観マーカー多め。期待度スコアは入れない。

【★見出しは固定文言ではなく、毎回違うことを書く★】
「## ぶっちゃけ気になる」「## こんな人なら買いかも」のような同じラベルを毎記事使わない。トピックに応じて自然な独白風の見出しにする。

以下の狙いのセクションを並べる:

1. **個人感情を出す導入の章**
   - 見出し例: 「実はこれ、結構気になってる」「これを見たとき、欲しいと思った」
2. **架空ユースケースの章** (生活シーン交えて3つ)
   - 見出し例: 「私ならこう使ってみたい」「通勤中にこれがあれば」「在宅の作業環境で活躍しそう」
3. **ワクワクポイントの章** (箇条書き、肌感ある言葉)
   - 見出し例: 「ここに惹かれたポイント」「触ってみたい部分」
4. **不安・懸念の章** (1〜2個)
   - 見出し例: 「でも、ここがちょっと不安」「悩ましいのは…」「決め手に欠ける点」
5. **対象読者の章** (会話っぽく)
   - 見出し例: 「こういう人は迷わず買って良い」「自分と同じタイプの人なら」
6. ## まとめ (固定OK)
7. ## よくある質問 (固定OK)""",

    'D': """【★この記事の構成タイプ: 批評型★】
賛否分かれる動きへの分析・論評。全体 **3000〜4500字**。論理的、対比明確、賛否並列。「と評価できる」「と考えるべきだ」のような論評トーン。期待度スコアは作らない。

【★見出しは固定文言ではなく、論じる対象を具体的に名指しする★】
「## 結論: これは賢い判断か?」「## 評価できる点」のような汎用ラベルは禁止。トピックの具体内容を含む見出しに。

1. **結論先出しの章** (冒頭で立場を3〜5行で明確に)
   - 見出し例: 「Apple のサブスク値上げは妥当な判断か」「この方針転換、私は反対だ」
2. **賛成側の論理の章** (2〜3個)
   - 見出し例: 「値上げを支持する3つの理由」「Apple の立場から見れば筋は通る」
3. **批判側の論理の章** (2〜3個)
   - 見出し例: 「それでも納得できない点」「ユーザー視点で見ると問題が多い」「業界への悪影響が懸念される」
4. **大きな文脈の章**
   - 見出し例: 「Spotify や Netflix の動きと並べて見る」「スマホ業界全体の流れの中で」
5. **中立的な勧告の章**
   - 見出し例: 「それでも継続を考える人へ」「乗り換え先を探すならこのあたり」
6. ## まとめ (固定OK)
7. ## よくある質問 (固定OK)""",

    'E': """【★この記事の構成タイプ: Q&A型★】
新しい技術・概念を分かりやすく解説。全体 **2800〜4000字**。期待度スコアは作らない。

【★H2見出しは全て「疑問形」にする。ただし毎回違う問いかけにする★】
「## ○○とは結局なに?」のように毎回同じ語順を使わない。読者が実際に検索しそうな多様な問いを並べる。

以下の狙いの問い6個を並べる(問いかけ表現は多様化):

1. **基本概念を問う章**
   - 例: 「そもそも Gemini Intelligence って何?」「Apple Vision Pro って結局どんなデバイス?」
2. **背景・時期を問う章**
   - 例: 「なぜ今このタイミングなのか」「Google が突然動き出したのはなぜ?」
3. **仕組み・中身を問う章**
   - 例: 「内部ではどう動いている?」「具体的にどんな処理がされる?」
4. **ユーザー影響を問う章**
   - 例: 「私たちの日常はどう変わる?」「実際の使い心地に違いは出る?」
5. **時期・条件を問う章**
   - 例: 「いつから日本でも使える?」「対応デバイスは限られるのか?」
6. **比較を問う章**
   - 例: 「ChatGPT との違いは結局どこ?」「Apple の AI と何が違うのか?」
7. ## まとめ (固定OK)
8. ## よくある質問 (本文の問いと重複しない3問)""",

    'F': """【★この記事の構成タイプ: ストーリー型★】
業界の流れや歴史を物語として読ませる。全体 **3500〜5000字**。「2020年に〜」「2024年には〜」のような時系列マーカーを多用。期待度スコアは作らない。

【★見出しは時系列の節目を具体的に表す。毎記事違う言い回し★】
「## ここに至るまでの経緯」「## 何が転機になったか」のような汎用ラベル禁止。年代やイベントを含む具体的な見出しに。

1. **過去の経緯の章**
   - 見出し例: 「2020年、最初の Switch 大ヒットから始まった」「iPad 初代の登場以前、業界はどうだったか」
2. **転機の章**
   - 見出し例: 「コロナ禍が決定的な変化をもたらした」「2023年のあの発表が分水嶺だった」
3. **現状の章**
   - 見出し例: 「いま、市場はこういう構造になっている」「2026年の今、各社の立ち位置」
4. **未来予想の章** (400〜600字)
   - 見出し例: 「3年後、この業界はこう動くと予想する」「2027年のスマホ市場を想像する」
5. **主観で締める章** (300〜500字)
   - 見出し例: 「個人的にこの流れをこう見ている」「ここまで追ってきた身として思うこと」
6. ## まとめ (固定OK)
7. ## よくある質問 (固定OK)""",

    'G': """【★この記事の構成タイプ: データ比較型★】
数字・スペック・表で勝負する記事。全体 **2800〜3800字**。表を最低2個使う。主観コメントは最小限、客観データ重視。期待度スコアは作らない。

【★見出しは比較対象を具体名で言う★】
「## スペック一覧」「## 数値で見る性能差」のような汎用ラベル禁止。比較対象の製品・モデル名を入れた見出しに。

1. **基本スペック表の章** (Markdown表で5〜10行)
   - 見出し例: 「Pixel 11 / Galaxy S26 / iPhone 17 のスペックを並べる」「3機種のサイズ・重量・価格を比較」
2. **性能数値の章** (ベンチマーク・実測値)
   - 見出し例: 「カメラ性能をDxOMarkで比較」「処理性能を Geekbench で確認」
3. **コスパ評価の章**
   - 見出し例: 「価格に対する性能を採点する」「コスパで選ぶならこれ」
4. **用途別おすすめの章** (表で整理)
   - 見出し例: 「動画用途で選ぶならどれが正解か」「使い方別の最適解」
5. **結論の章**
   - 見出し例: 「総合評価: 結局この3機種、どれを選ぶべきか」「コスパ・性能・デザインで決まり手」
6. ## よくある質問 (固定OK)""",
}


_TYPE_CLASSIFY_PROMPT = """以下の記事トピックに最も合う「記事タイプ」を A〜G の中から1つだけ選んでください。

【トピック名】
{topic_name}

【概要】
{topic_summary}

【ソース記事タイトル】
{source_titles}

【記事タイプ7種】
A. 速報型 — 「○○が発表された」「○○が発売」など、結論先出しで簡潔に伝えるべきニュース
B. 深掘り解説型 — 大型新製品レビュー、じっくり論じる価値ある特集
C. 体験予想型 — 個人的に欲しい製品、自分の使い方を語れる物
D. 批評型 — 賛否が分かれそうな企業判断、ポリシー変更、論争を呼ぶ動き
E. Q&A型 — 新しい技術・概念の解説、仕組みの整理が必要なテーマ
F. ストーリー型 — 業界の動向、過去から未来への流れを語れるテーマ
G. データ比較型 — スペック比較、ベンチマーク、価格 vs 性能の評価

【出力】A、B、C、D、E、F、G のいずれか1文字のみ。理由・説明・前置きは不要。"""


def classify_article_type(topic_name, topic_summary, sources):
    """Gemini にトピックの性質を見せて、最適な記事タイプ(A〜G)を選ばせる。

    失敗時は 'B'(深掘り解説型・無難)にフォールバック。
    コスト: 1呼び出しあたり ~10 tokens、Flash Lite で 1記事 0.001円未満。
    """
    source_titles = '\n'.join(
        f'- {s.get("title", "")}' for s in (sources or [])[:5]
    ) or '(なし)'
    prompt = _TYPE_CLASSIFY_PROMPT.format(
        topic_name=(topic_name or '').strip(),
        topic_summary=(topic_summary or '').strip(),
        source_titles=source_titles,
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or '').strip().upper()
    except Exception as e:
        print(f'[classify-type] failed: {e}, fallback=B', flush=True)
        return 'B'

    for ch in 'ABCDEFG':
        if ch in text[:5]:  # 先頭5文字以内
            return ch
    print(f'[classify-type] no valid letter in "{text[:30]}", fallback=B', flush=True)
    return 'B'


ARTICLE_PROMPT = """あなたは日本のガジェットブログ「たこちゃん、今日もガジェットに絡まる。」の記事作成担当です。海外の最新ガジェット情報を、日本人読者の検索意図にちょうどよく応える記事を書きます。

# 【★★★ 書き手の立場 (最重要・これを外したら記事失格) ★★★】

あなたは **ガジェット好きの個人ブロガー** で、海外の新製品情報を毎日チェックしている。
**ガジェット雑誌の記者**でも **業界アナリスト**でも **解説サイトの執筆者**でもない。

書く記事は **「私が最近見つけて気になっているガジェットを、買おうか迷いながら調べた個人ノート」**。
読者は「他人が買うか迷ってる過程を覗き見したい」「自分の購入判断の参考にしたい」人。

▼ この立場の核 (絶対遵守):
- **起点は「私が今これに感じていること」**:「3日前に発表を見て、それ以来ずっと気になっている」「Twitter で見かけて、調べ始めた」「先週から欲しくて悩んでいる」のように、私の物欲ストーリーから書き始める
- **内容は「私の購入検討プロセス」**: 「私が気になった3つの点」「私が並行検討している競合」「私の使い方想像」「私が引っかかっている懸念」「現時点での私の結論」
- **客観的な業界解説は最小限**: スペックや競合データは「私の判断材料」として置く。「市場動向」「業界では」「専門家によると」のような第三者目線は禁止

▼ ニュース記事化禁止フレーズ (即失格):
- 「本記事では〜について整理する/解説する/紹介する」
- 「業界では〜の動きが活発化している」
- 「市場では〜が注目されている」
- 「専門家によると」「アナリストの見立てでは」
- 「以下のメリットが挙げられる」「以下のような特徴がある」
- 「本製品は〜という側面を持つ」
- 「結論から言うと、このデバイスは〜として機能する」

▼ 正解の起点パターン (これに準じた書き出しを毎回考える):
- 「**先週、X 社が** 新しい AR グラスを発表した。それ以来、私はこれを買おうか3日悩んでいる。」
- 「**Twitter で見かけて** 気になって調べ始めた○○。実機レビューはまだないが、スペックを並べると私の使い方には刺さりそう。」
- 「**Kickstarter で偶然見つけて**、思わずブックマークした。届くのは半年後らしいが、いまから検討メモを残しておく。」
- 「**もうすぐ XREAL の新モデルが出る** らしい。私は前モデルを買い損ねた後悔があるので、今回は早めに準備したい。」

▼ 各セクションも「私の視点」で書く (v2 SEO 構造を守りつつ立場固定):
- 「公式スペック表」→ 私が見て「ここが決め手」と感じる項目を強調
- 「なぜ今この製品なのか」→ 私がいま欲しいと思った個人的な理由
- 「注目ポイント」→ 私がワクワクした3点
- 「競合比較」→ 私が並行検討している競合機との違い
- 「活用シナリオ」→ 私が想像する自分の使い方
- 「懸念点」→ 正直、私が引っかかっている2点
- 「向いてる人」→ 私と同じ悩みの人へのメッセージ
- 「まとめ」→ 現時点での私の結論 (買う/見送る/待つ)

# 【★最優先・違反したら記事失格★】

これらは記事の最重要要件。書き終わったら必ずセルフチェックすること:

1. **口調は常体 (だ・である・と思う・じゃないか・な気がする) で全文書く**。
   - **ですます調は使わない** (FAQ の「A. 〜」回答だけ例外で許可)
   - 「〜です」「〜ます」を本文中で書いたら即失格。
   - 例: ✅「価格は40万円。正直、高い。」❌「価格は40万円です。少し高めです。」
2. **本文は 3,000〜3,800字** (これを超えてはいけない)
3. **タイトル(1行目)は 30文字以内**、以下の単語は **絶対に入れない**:「徹底」「完全ガイド」「2026年版」「最新」「!」「?」「【」「】」「[」「]」(空括弧は禁止)
4. **タイトルの直後 (2行目以降) に必ず `検索意図: Buy` / `検索意図: Know` / `検索意図: Do` / `検索意図: Go` のいずれかを1行で書く**
5. **末尾に必ず `## 情報ソース` セクションを置き、公式URLを箇条書きで並べる**
6. **FAQ は `## よくある質問` セクション内に `### Q. 〜` / `A. 〜` 形式で 5問**
7. **H2見出しは最大8個まで**

【今回扱う製品】
- 製品名: {topic_name}
- 想定検索キーワード: {target_keyword}
- 公式情報URL: {official_url}
- 参考リンク: {reference_urls}

# 【最重要】記事を書く前に判定すること

このキーワードで検索する人は、4つの検索意図のうちどれに当たるか必ず判定する:

- **Know(知りたい)**: 「{topic_name} とは」「{topic_name} できること」→ 仕組み・機能解説型
- **Go(行きたい)**: 「{topic_name} 公式」「{topic_name} 日本サイト」→ 入り口案内型
- **Do(したい)**: 「{topic_name} 使い方」「{topic_name} 設定」→ ハウツー型
- **Buy(買いたい)**: 「{topic_name} 価格」「{topic_name} レビュー」「{topic_name} vs」→ 購入検討型

→ 出力の中で「**検索意図: ◯◯**」を導入直後に1行明記してから本文を続ける。

# 記事の必須要件

## 字数

**3,000〜3,800字**。これを超えてはいけない。網羅的に書こうとせず、検索意図に「ちょうど」答える。

## 検索意図別の構成テンプレ

### Buy(購入検討型)の場合

1. 冒頭(150字): 「誰の、どんな悩みを解決する製品か」一文
2. この記事でわかること(箇条書き3つ)
3. 公式スペック表(項目10以上、価格と発売日必須)
4. なぜ今この製品なのか(400字以内、市場文脈)
5. 注目している3つのポイント(番号付き、各2〜3文)
6. 競合3製品との比較表(価格・主要スペック・特徴)
7. 具体的な活用シナリオ(箇条書き3つ)
8. 正直な懸念点2つ(番号付き、各2〜3文)
9. こんな人に向いている / やめておくべき人(対比2つ)
10. まとめ(150字以内)
11. FAQ(5問、`### Q. 〜` / `A. 〜` 形式厳守)

### Know(知りたい型)の場合

1. 冒頭(150字): 「結論を一文で先出し」
2. この記事でわかること(箇条書き3つ)
3. ◯◯とは何か(300字、定義と概要)
4. 仕組み(見出し2〜3個でブロック分け)
5. できること・できないこと(対比表)
6. 似た技術・サービスとの違い(比較表)
7. いつから使えるか・対応状況(具体的な日付・国・端末)
8. まとめ(150字以内)
9. FAQ(5問、`### Q. 〜` / `A. 〜` 形式厳守)

### Do(ハウツー型)の場合

1. 冒頭(150字): 「この記事を読み終えるとできるようになること」
2. 必要なもの(箇条書き)
3. 手順(番号付きステップ、各ステップに見出し)
4. つまづきやすいポイント3つ
5. うまくいかない時の対処
6. まとめ(150字以内)
7. FAQ(5問、`### Q. 〜` / `A. 〜` 形式厳守)

### Go(入り口案内型)の場合

1. 冒頭(150字): 「どこへ行けば公式情報があるか」結論先出し
2. 公式サイト一覧(国別・用途別の正規URL)
3. 公式情報で確認できること
4. 偽サイト・並行輸入サイトの見分け方
5. まとめ(150字以内)
6. FAQ(5問)

# 文体ルール (絶対遵守) — 自分の感想を書く個人ブロガー口調

## 文体の基本

会社員的・解説サイト的・PR記事的なトーンは徹底排除。ガジェット好きの人が自分の備忘録として書く感じ。
**特定の有名ブロガーの言い回しをそのまま真似ない** (盗用扱いになる)。下記は方向性として参考にしつつ、自分の言葉で書く:

**A. 常体ベース、ですます調は使わない (FAQ 回答だけ例外)**
- 「である」「〜のはず」「〜と思う」「〜じゃないか」「〜な気がする」を自然に混ぜる
- 文末を「である」一辺倒にせず、バリエーションを持たせる

**B. 一人称「私」を時々入れる** (毎セクション必須ではない)
- 「私は」「私だったら」「私の感覚では」を、感想や判断の場面で2〜4回程度
- 連発するとうるさいので控えめに
- 例:「私はこの厚みは机に置きづらいと思った」「私の用途なら充電は週1で足りる」

**C. 短文 + 長文の自然なミックス**
- 短文 (10〜30文字) で結論や印象を区切る:「これは買い」「正直、悩む」「価格はキツい」
- そのあと長文で背景や理由を補足
- 1段落 = 1〜3文、120字以内

**D. 接続詞のバリエーション**
- 「とはいえ」「ただ」「それでも」「逆に」「一方で」など、流れを変える接続詞を活用
- 持ち上げて落とす、落として持ち上げるリズムを作る
- 同じ接続詞を連発しない

**E. 個人体験ベースで書く**
- 「私の机では...」「自宅で使うなら...」のような、具体的な状況設定
- 一般論を語るときも「自分が買うか買わないか」の視点を残す

**F. 砕けた語を控えめに混ぜる** (1記事 3〜6回程度)
- 「正直」「ぶっちゃけ」「ガチで」「微妙」「ヤバい」「割と」「結構」
- 連発するとブログ全体が品が無くなる
- 公序良俗に反する語・極端な俗語 (「うんこ」「魑魅魍魎」のような特定ブロガー独特語) は禁止

## 使用禁止フレーズ (AI臭・解説サイト臭)

- 「気になっている」「気になります」 (他人事感)
- 「公式情報によると」「公式情報では」 (官製文書感)
- 「先行レビューでは〜のようです」
- 「期待しています」「期待したい」
- 「〜と予想されます」「〜と思われます」「〜と考えられます」
- 「実機が出たら試してみたい」
- 「徹底解説」「完全ガイド」「徹底比較」 (タイトル・本文両方禁止)
- 「非常に」「画期的」「革新的」「素晴らしい」「圧倒的」 (中身ゼロの修飾語)
- 「皆さん」「読者の皆様」 (固い呼び方)
- 「〜することができる」「〜することによって」「〜において」 (官僚文書語彙)

## 書き方の原則

- **断定形で書く**(例:「Rokid は 49g で軽い」「これは買わない」)
- 不確実な情報は「**公式は未公表**」と明記、推測で埋めない
- 同じ情報を繰り返さない
- 「私の感想」を抑えない。むしろ全開で出す。「私は気に入った」「私は微妙だと思う」を遠慮なく書く

## 口調の参考例 (このトーンを目指す)

✅ 目指す例:
- 「ARグラス、思ったより普及してない印象がある。最近の新製品は完成度が上がってきていて、私もそろそろ1台買おうか悩んでいる。」
- 「スペック表だけ見ると強い。ただ、この価格で買う人がどれくらいいるかは正直読めない。」
- 「重量は360g。腕時計より重い計算になるから、1日中つけるのは個人的に無理だと思う。」

❌ NG 例:
- 「ARグラス市場は今後拡大が見込まれます。注目していきたい分野です。」 (解説サイト感)
- 「本製品は革新的な機能を備えており、ユーザー体験を大きく向上させます。」 (官製文書)
- 「気になる方は公式情報をご確認ください。」 (他人事)

# 独自情報・E-E-A-T 要件 (これがないと SEO 評価されない)

## 必ず1つ以上含める「独自視点」

以下のどれか **最低1つ** を記事内に組み込む:

- 海外フォーラム (Reddit / 海外メディア) で語られている実ユーザーの声を1つ引用
- 競合との「**数値で比較できる差**」を1つ明示 (例: バッテリーが 18% 長い)
- 日本市場特有の課題への言及 (例: 技適、日本語対応、楽天/Amazon 並行輸入の価格差)
- 他のブログが触れていない盲点 (例: 修理保証の有無、サポート言語、技適マーク)

## E-E-A-T の担保

- 数値・スペックは必ず **一次情報 (公式サイト)** から引用
- 出典が不明な情報は書かない (「公式は未公表」と書く)
- 記事末尾に **「情報ソース」セクション** を設け、公式URLを箇条書きで明記

# 見出しルール

- 見出しは **最大8個**
- 見出しに「2026年版」「徹底比較」「選び方完全ガイド」を入れない
- 自然な日本語で短く

例:
- ✕ 悪い: 「Rokid Air vs XREAL Air2 Pro 度付きレンズの選定基準2026年版」
- ◯ 良い: 「度付きレンズはどう選ぶか」

# 表のルール

- スペック表と比較表は必ず入れる
- それ以外で表を多用しない (本文中の表は **最大2個まで**)

# タイトル生成ルール

タイトルは検索意図に合わせる:

**Buy 型**:
- 「{topic_name} レビュー。{特徴}が変える{用途}」
- 「{topic_name} vs {競合}。違いと選び方」
- 「{topic_name} の価格と日本での買い方」

**Know 型**:
- 「{topic_name} とは何か。仕組みとできることをまとめた」
- 「{topic_name} は何が新しいのか」

**Do 型**:
- 「{topic_name} の使い方。初期設定から実用まで」
- 「{topic_name} を{目的}で使う手順」

タイトルに含めないもの (違反は失格):
- 「徹底」「徹底解説」「徹底比較」「徹底調査」 (「徹底」を含む語は全て禁止)
- 「完全ガイド」「2026年版」「最新」
- 「!」「?」を3回以上
- 30文字超え

# 出力形式 (厳格)

Markdown 形式で、以下の順序で出力する:

1. **1行目**: `# タイトル` (上のタイトル生成ルールに従う)
2. **2行目**: 空行
3. **3行目**: `検索意図: Buy` / `検索意図: Know` / `検索意図: Do` / `検索意図: Go` のいずれか
4. **4行目**: 空行
5. **本文** (検索意図別テンプレに従う、3000-3800字)
6. **本文末尾**: `## 情報ソース` セクション (公式URLの箇条書き)

前置き・コードフェンス・「以下が記事です」のような説明は一切書かない。

{official_image_section}{wiki_images_section}{product_card_block}{related_articles_section}

【参考情報 (これらから事実だけ抽出し、表現は全て独自に書く)】
{sources}

それでは記事を書いてください。
"""


CHART_INSTRUCTIONS = """
【グラフの挿入 (任意・記事中1〜2個まで)】
数値データを視覚化したい箇所に、以下の形式で記述すること。コード側で自動的にグラフ画像に変換される。

書き方:
[CHART type="TYPE" title="タイトル" labels="ラベル1,ラベル2,ラベル3" values="数値1,数値2,数値3" unit="単位"]

TYPEの種類:
- bar  : 棒グラフ（売上・業績推移など）
- line : 折れ線グラフ（株価・トレンドなど）
- pie  : 円グラフ（構成比・シェアなど）

unitの例: "億円" / "万台" / "%" / "円" / "百万ドル"
- bar・lineではY軸ラベルに表示される
- pieではタイトルに "(単位: ○○)" として表示される
- 単位が不明・不要な場合は unit="" または省略可

ルール:
- labelsとvaluesの個数を必ず一致させる
- valuesは純粋な数値のみ（単位・カンマ・記号不可）
- 参考情報に明記されている数値のみ使用。推測・架空の数値は絶対に使わない
- データが不確かな場合はグラフを挿入しない
"""

_OWNERSHIP_NOT_OWNED = """
【★★★ 重要: 所有状況の前提 — 必ず守ること ★★★】
あなた(ブロガー)は **この製品/トピックについて、まだ実機を所有していません**。実機を触ったことがありません。
記事は「気になっている読者と一緒に発表内容を整理する」スタンスで書いてください。

書き方の絶対ルール:
- 「実際に使ってみると」「1週間試した」「持った感じ」「触ってみると」のような **実体験を装った表現は絶対に使わない**
- 物理的な体験は書かない: 重量感・手触り・操作感・ボタンの押し心地・画面の発色・音質・匂い・温度感など (見たことが無いから書けるはずがない)
- 代わりに以下の表現を必ず使う:
  - 「発表内容を見ると〜のようです」「公式情報によると〜」
  - 「気になっているのは〜」「実機が出たら試してみたい」
  - 「先行レビューでは〜と評価されているようです」
  - 「もし買ったら〇〇に使ってみたい」「期待しているポイントは〜」
- 見出しも未所有者目線でOK: 「気になるポイント3つ」「期待しているスペック」「購入を検討している人へ」
- 客観的なスペック整理 + 期待感 + 購入検討者向けの情報整理が中心
"""


_OWNERSHIP_OWNED = """
【★★★ 重要: 所有状況の前提 — 必ず守ること ★★★】
あなた(ブロガー)は **この製品を実際に所有して使用しています**。実機レビューとして書いてください。

書き方の絶対ルール:
- 実体験ベースで書く: 「使ってみると○○だった」「1週間使った感想」「気になったのは△△」「測ってみたら〜」
- 物理的な観察を必ず2箇所以上含める: 重量感・手触り・操作感・ボタンの押し心地・画面の発色・音質・温度感・匂い・実際の挙動など
- 生活シーンに結びつける: 「通勤中に〜」「自宅で使うと〜」「週末のカフェで試したら〜」「運動中に〜」など
- 「測ってみた」「確認したら○○だった」のような検証的な記述
- 良かった点と気になった点を実際の使用感に基づいて両方書く
"""


def _ownership_section(ownership):
    if ownership == 'owned':
        return _OWNERSHIP_OWNED + '\n'
    if ownership == 'not_owned':
        return _OWNERSHIP_NOT_OWNED + '\n'
    return ''


AMAZON_INSTRUCTIONS = """
【Amazon リンク】
- **Amazon.co.jp で実際に購入できる物理的な商品** に言及しているときに限り、Amazon検索URLを貼る。
- 形式: `[商品名 を Amazon で探す](https://www.amazon.co.jp/s?k=商品名)`
- 検索URLのみ使用 (`https://www.amazon.co.jp/s?k=検索キーワード`)。ASINや商品ページ直URLは使わない（存在しないURLになるため）。
- キーワードは商品名をそのまま使う（例: `iPhone 16 Pro`、`ソニー WH-1000XM5`）。
- **以下のものには絶対にリンクを貼らない**:
  - サービス・サブスク・アプリ・ソフトウェア・SaaS・Webサービス
  - 自動車・住宅・不動産
  - 飲食店・チェーン店・食品サービス
  - 法人/B2B限定品、海外限定品、未発売品、コンセプト品
  - 企業名・ブランド名そのもの（特定の販売中の製品でなければNG）
  - 一般名詞だけで具体的な型番が無いもの（例: 「スマートフォン全般」など）
- **無理にリンクを貼らないこと**。該当する商品が無ければ 0 個で構わない。多くても 2 個まで。記事の自然さを優先する。
"""


RAKUTEN_INSTRUCTIONS = """
【楽天市場リンク】
- Amazonリンクを貼る商品については、**並べて楽天の検索URLも必ず貼る** (購入チャネル選択肢を増やす)。
- 形式: `[商品名 を Amazon で探す](https://www.amazon.co.jp/s?k=商品名) / [楽天で探す](https://search.rakuten.co.jp/search/mall/商品名/)`
- 楽天URLは **「search.rakuten.co.jp/search/mall/<キーワード>/」固定**。商品ID直リンクは禁止(存在しないURLになるため)。
- キーワードは Amazon と完全一致(商品名そのまま)。
"""


PRODUCT_CARD_INSTRUCTIONS = """
【★★★ 商品カード(画像+Amazon/楽天ボタン) — 収益化の要、必ず最低2個入れる ★★★】
このブログは収益化を最重要視している。**1記事あたり商品カードを最低2個、できれば3〜5個 必ず挿入する**。
コード側で楽天APIから実商品の画像・価格・アフィリエイトURLを取得して、視覚的な「画像+価格+Amazonボタン+楽天ボタン」のカードに自動変換される。

**書き方 (この通り、本文中の独立した行に書く)**:
```
[PRODUCT_CARD: 商品名]
```

**最低2個入れるための考え方** — 主題が直接買える物でなくても、必ず関連商品でカードを稼ぐ:

▼ 主題が**ロボット/AI/フロンティア技術**(直接買えない題材)の場合:
- 「これに近いものを家庭で体験できる関連ガジェット」を提示してカード挿入
- 例: Atlasロボットの記事 → ロボット掃除機 (Roomba) / スマートホーム (Echo) / 自動運転玩具
- 例: 生成AI記事 → AIスピーカー / AI機能付きカメラ / AIノイキャンイヤホン
- 例: ARグラス記事 → 比較対象の他社ARグラス(XREAL, VITURE等)、関連アクセサリ

▼ 主題が**スマホ/PC/家電**(買える題材)の場合:
- 製品本体カード + 関連アクセサリカード(ケース、充電器、ストレージ等)
- 競合機種カード(比較セクションで自然に置く)

▼ 主題が**業界ニュース/政策/サービス**の場合:
- 「このニュースで関連性が高いガジェット」を1〜2個紹介して必ず挿入
- 例: スマホ値上げニュース → 旧型スマホ・SIMフリー機種・モバイルバッテリー

**具体例**:
- `[PRODUCT_CARD: Sony WH-1000XM5]`
- `[PRODUCT_CARD: iPhone 17 Pro 256GB]`
- `[PRODUCT_CARD: Anker PowerCore 10000]`
- `[PRODUCT_CARD: ルンバ j7]`
- `[PRODUCT_CARD: XREAL One]`
- `[PRODUCT_CARD: Jackery 1000 Plus]`

**ルール**:
- 商品名は **楽天市場で検索してヒットする一般的な型番** にする (型番・メーカー名・モデル名込み)
- カードは「## H2見出し」直下 or 製品に言及した段落直下の独立した行に置く(前後に空行)
- **「## 関連商品」「## 一緒に検討したいガジェット」「## こちらもおすすめ」のような専用H2セクションを1つ作って2〜3カード並べる**のも推奨
- 同じ商品のカードを記事内に2回入れない (型番違いは別商品扱いOK)
- カード以外で Amazon URL や 楽天 URL を本文に直接書かない (テキストリンクは使用禁止)

**絶対にカードを使わないもの** (検索しても無関係な結果が出るため):
- サービス・サブスク・アプリ・SaaS・Webサービス
- 自動車・住宅・不動産・飲食店・食品サービス
- 法人/B2B限定品、未発売品、コンセプト品
- 抽象的な一般名詞 (「スマートフォン全般」のような型番なし)

**プレースホルダーが楽天で見つからなければ自動的に削除される**ので、迷ったら積極的に書いて良い。
ただし「最低2個」は記事の必須要素。0個・1個で記事を完結させない。
"""


def build_article_prompt(topic_name, topic_summary, sources, tone_prompt='', genre='ブログ', amazon_affiliate_tag='', rakuten_affiliate_id='', extra_instructions='', article_policy='', use_charts=False, wiki_images=None, official_image=None, ownership='unspecified', related_articles=None, longtail_keywords=None, use_product_cards=False, article_type=None):
    """ARTICLE_PROMPT を完成形に組み立てて返す (Gemini送信 or 外部LLMへの引き渡し用)。

    article_type=None の場合のみ classify_article_type を呼んで自動判定。
    既に分かっていれば渡すことで Gemini 呼び出しを節約できる。
    """
    persona_intro = f'あなたは経験豊富な日本の{genre}ブロガーです。'

    policy_section = ''
    if article_policy and article_policy.strip():
        policy_section = (
            '【★最優先ルール★ — 以下の全指示より優先される。必ず守ること】\n'
            f'{article_policy.strip()}\n\n'
        )

    tone_section = ''
    if tone_prompt and tone_prompt.strip():
        tone_section = f'【口調・テイスト】\n{tone_prompt.strip()}\n\n'

    extra_section = ''
    if extra_instructions and extra_instructions.strip():
        extra_section = f'\n【ユーザーからの追加指示 (最優先で反映)】\n{extra_instructions.strip()}\n'

    longtail_section = ''
    if longtail_keywords:
        # longtail_keywords は [{'theme': '...', 'reason': '...'}] or [str] のどちらか許容
        normalized = []
        for k in longtail_keywords:
            if isinstance(k, dict):
                t = (k.get('theme') or '').strip()
                if t:
                    normalized.append(t)
            elif isinstance(k, str):
                t = k.strip()
                if t:
                    normalized.append(t)
        if normalized:
            kw_lines = '\n'.join(f'  - {t}' for t in normalized[:5])
            longtail_section = (
                '\n【★ ロングテールキーワード — SEO最適化のため本文に自然に含めること ★】\n'
                '以下の検索キーワード(検索意図)を、本文中に**自然な日本語の文脈で**散りばめてください:\n'
                f'{kw_lines}\n'
                '- 不自然な羅列・キーワード詰め込みは厳禁。読者の悩みに刺さる形で1〜2回ずつ織り込む。\n'
                '- 「H2見出し or H3見出しの少なくとも1つにこれらキーワードのいずれかを含める」と尚良い。\n\n'
            )

    # 楽天APIで実商品を取得して画像付きカードを出せる場合は、テキストリンクではなくカードを使う
    if use_product_cards:
        amazon_section = PRODUCT_CARD_INSTRUCTIONS
    else:
        amazon_section = AMAZON_INSTRUCTIONS if amazon_affiliate_tag else ''
        if amazon_affiliate_tag and rakuten_affiliate_id:
            amazon_section += RAKUTEN_INSTRUCTIONS
    chart_section = CHART_INSTRUCTIONS if use_charts else ''

    # 記事タイプ判定 (既に渡されてれば再判定しない)
    if article_type is None:
        article_type = classify_article_type(topic_name, topic_summary, sources)
        print(f'[article-gen] type={article_type} for "{(topic_name or "")[:40]}"', flush=True)
    type_section = _TYPE_INSTRUCTIONS.get(article_type, _TYPE_INSTRUCTIONS['B'])

    official_image_section = ''
    if official_image and official_image.get('url'):
        maker = official_image.get('maker') or '公式サイト'
        page_url = official_image.get('page_url') or ''
        img_url = official_image['url']
        is_press = bool(official_image.get('is_press_photo'))
        alt_text = f'{maker} 公式サイトより' if is_press else f'{maker} 報道写真'
        kind_label = '公式サイト画像' if is_press else 'ニュース記事の画像 (引用扱い)'
        official_image_section = (
            f'\n【{kind_label} — ★必ず使用すること★】\n'
            f'- 画像URL: {img_url}\n'
            f'- 出典ページ: {page_url}\n'
            f'- 出典名: {maker}\n'
            '**この画像は記事の冒頭付近 (最初のH2より前、または導入文直後) に必ず1枚埋め込むこと**。省略不可。\n'
            f'埋め込み形式 (この通りそのまま書くこと):\n'
            f'```\n![{alt_text}]({img_url})\n> 引用元: [{maker}]({page_url})\n```\n'
            '画像URLや引用元は改変しないこと。\n'
        )

    related_articles_section = ''
    if related_articles:
        lines = ['\n【関連する過去記事 (内部リンクとして必ず使用)】']
        lines.append('以下は同じブログの関連記事です。読者の回遊性を高めるため、必ず以下の2点を実行:')
        lines.append('1. **本文中で自然な箇所に1〜2個リンクする** (例: 「以前の記事では○○について書きました」)')
        lines.append('2. **記事末尾に「## 関連記事」セクションを追加し、全件を箇条書きでリンク**')
        lines.append('')
        for r in related_articles:
            lines.append(f'- [{r["title"]}]({r["url"]})')
        related_articles_section = '\n'.join(lines) + '\n'

    wiki_images_section = ''
    if wiki_images:
        lines = ['', '【Wikimedia Commons 画像 (著作権フリー・使用推奨)】']
        lines.append('以下の画像は自由に使用できます。記事の適切な箇所に1〜2枚埋め込んでください。')
        lines.append('埋め込み形式: `![説明](画像URL)` の直後に必ず以下の著作者表示を入れること:')
        lines.append('`> 画像: [著作者名](ページURL) / ライセンス名`')
        lines.append('')
        for img in wiki_images:
            lines.append(
                f'- 画像URL: {img["url"]}\n'
                f'  著作者: {img["credit"]} / ライセンス: {img["license"]}\n'
                f'  ページURL: {img["page_url"]}'
            )
        wiki_images_section = '\n'.join(lines) + '\n'

    parts = []
    for i, s in enumerate(sources):
        # 画像URLは意図的に渡さない (Gemini がソース画像を埋め込んでしまうのを防ぐ)
        chunk = (
            f'■ ソース{i+1}: {s["title"]} ({s["source"]})\n'
            f'  記事URL: {s["url"]}\n'
            f'  本文:\n{s["text"]}'
        )
        parts.append(chunk)
    sources_text = '\n\n'.join(parts)

    # v2 用のフィールド準備
    # 想定検索キーワード = topic_name と topic_summary を組み合わせ
    target_keyword = topic_name
    # 公式URL候補
    official_url = ''
    if official_image and official_image.get('page_url'):
        official_url = official_image['page_url']
    elif sources:
        for s in sources:
            if s.get('url') and 'hatenablog.com' not in (s.get('url') or ''):
                official_url = s['url']
                break
    # 参考URL一覧
    reference_urls = '\n'.join(f'- {s["url"]}' for s in sources if s.get('url')) or '(なし)'

    # 商品カードの埋め込み指示 (v2 は明示していないが収益化要件)
    product_card_block = ''
    if use_product_cards:
        product_card_block = (
            '\n\n【★ 商品カードと誘導文 (収益化の必須要件) ★】\n'
            '本文中で具体的な物理商品 (Amazon・楽天で買える型番) に言及した直後に '
            '`[PRODUCT_CARD: 商品名]` を独立行で **1〜2個** 入れる。\n'
            '\n'
            '★ カードの **直前** に必ず「誘導の1文」を入れる。カード単独で置かない。\n'
            '誘導文の例 (毎回違う言い回しを使う、テンプレ感を出さない):\n'
            '- 「気になる人は実勢価格と在庫を見ておきたい。」\n'
            '- 「実物のサイズ感や色を確認したいなら下のカードから。」\n'
            '- 「現行モデルは下のリンクで実際の価格をチェックできる。」\n'
            '- 「店頭で見ない人はこちらで型番を直接確認しておくと早い。」\n'
            '- 「並行輸入や旧モデルとの価格差を比較するなら下のリンクが速い。」\n'
            '\n'
            '★ カードは「## まとめ」より前、「## FAQ」より前の位置に配置する。\n'
            '★ 商品名は楽天で確実にヒットする一般的な型番 (例: 「XREAL One」「Meta Quest 3」「Anker PowerCore 10000」「VITURE One Lite」)。\n'
            '★ 楽天で検索ヒットしない商品名は自動的に削除されるので、書いて損はない。\n'
            '★ 押し売り感は禁止。「読者にとってこれは見ておく価値がある」程度のトーン。\n'
        )

    prompt = (
        ARTICLE_PROMPT
        .replace('{official_image_section}', official_image_section)
        .replace('{wiki_images_section}', wiki_images_section)
        .replace('{product_card_block}', product_card_block)
        .replace('{related_articles_section}', related_articles_section)
        .replace('{topic_name}', topic_name)
        .replace('{target_keyword}', target_keyword)
        .replace('{official_url}', official_url or '(未指定)')
        .replace('{reference_urls}', reference_urls)
        .replace('{sources}', sources_text)
    )
    return prompt, article_type


def generate_article(topic_name, topic_summary, sources, tone_prompt='', genre='ブログ', amazon_affiliate_tag='', rakuten_affiliate_id='', extra_instructions='', article_policy='', use_charts=False, wiki_images=None, official_image=None, ownership='unspecified', related_articles=None, longtail_keywords=None, use_product_cards=False):
    prompt, article_type = build_article_prompt(
        topic_name, topic_summary, sources,
        tone_prompt=tone_prompt, genre=genre,
        amazon_affiliate_tag=amazon_affiliate_tag,
        rakuten_affiliate_id=rakuten_affiliate_id,
        extra_instructions=extra_instructions,
        article_policy=article_policy,
        use_charts=use_charts, wiki_images=wiki_images,
        official_image=official_image, ownership=ownership,
        related_articles=related_articles,
        longtail_keywords=longtail_keywords,
        use_product_cards=use_product_cards,
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    md = _generate_with_search(client, prompt)

    if md.startswith('```'):
        md = _strip_code_fence(md)

    if use_charts:
        md = _convert_chart_markers(md)

    title, body = _split_title(md)

    if amazon_affiliate_tag:
        body = _apply_amazon_affiliate(body, amazon_affiliate_tag)
    if rakuten_affiliate_id:
        body = _apply_rakuten_affiliate(body, rakuten_affiliate_id)
    if amazon_affiliate_tag or rakuten_affiliate_id:
        body = '> ※本記事には広告が含まれます。\n\n' + body

    return title, body


_COMPARISON_PROMPT = """{policy_section}{tone_section}{ownership_section}
{persona_intro}以下の製品/サービスを比較するブログ記事を書いてください。

【比較対象】
{products_block}

{extra_section}
【共通ルール】
- 参考情報があればそこから事実を抽出。無ければあなたの知識で書く。架空のスペックは書かない。
- 数値・固有名詞・発売日は正確に。確証がない情報は「とされています」で表現。
- ブロガー個人の経歴・職業など記事と無関係な自己紹介は書かない。

【記事構成 (必ず従うこと)】
1. # タイトル — 「{title_pattern}」を意識した28〜38文字のSEOタイトル
2. 導入 (約150文字) — 何と何を比較するか、結論サマリ1文
3. **`[:contents]` を1行で記述**(導入直後・最初のH2より前)→ はてな目次自動生成
4. ## 一目でわかる比較表 — 必ずMarkdown表で全製品を横並び比較。行は最低: 価格 / 発売日 / 主要スペック3〜5項目 / 特徴
5. 各製品のH2セクション (## 製品名 の特徴) — 各製品ごとに1セクション。良い点3つ・気になる点2つを箇条書き
6. ## どっちを選ぶべき？ (用途別おすすめ) — 「Aがおすすめな人」「Bがおすすめな人」を箇条書き
7. ## まとめ
8. ## よくある質問 (`### Q. 〜` / `A. 〜` 形式厳守) — 3問程度

【視覚的見やすさ (必ず実施)】
- 重要キーワード・結論は `<mark>...</mark>` で黄色ハイライト (各H2で0〜2箇所)
- 価格・スペック・型番は `**太字**` で強調
- 各H2セクション末尾に `> ポイント:〜` の引用ブロックで要点まとめ
- 段落は100〜200文字、だらだら長文NG

【その他ルール】
- 絵文字・記号アクセント禁止。装飾は太字・mark・blockquote・表・箇条書きのみ
- 出力はMarkdownのみ。1行目は「# タイトル」。前置き・コードフェンス不要
- 全体2500〜4000文字
{official_images_section}{amazon_section}
{sources_block}
それでは記事を書いてください。
"""


_RANKING_PROMPT = """{policy_section}{tone_section}{ownership_section}
{persona_intro}以下のテーマに沿った「ランキング記事」を書いてください。

【テーマ】
{theme}

【ランキングのルール】
- {rank_count} 位までランキング形式で紹介する。
- 各順位の製品/サービスは実在するものを選ぶ。架空の製品名は厳禁。
- スペック・価格は確証がある場合のみ書く。確証が無ければ「販売店により異なります」「公式情報を確認」等で逃げる。

{extra_section}
【記事構成 (必ず従うこと)】
1. # タイトル — 「{theme} おすすめ{rank_count}選 2026年版」のような検索狙いタイトル (28〜38文字)
2. 導入 (約150文字) — 選定基準と結論サマリ
3. **`[:contents]` を1行で記述**(導入直後・最初のH2より前)→ はてな目次自動生成
4. ## ランキング選定基準 — どんな観点で選んだか3〜5個の箇条書き
5. ## 第N位: 製品名 (Nは大きい順から) — 各製品にH2セクション。最低{rank_count}個。各セクションに:
   - 1〜2文の概要
   - **おすすめポイント (箇条書き3〜4個)**
   - **気になる点 (箇条書き1〜2個)**
   - 「こんな人におすすめ」1行
6. ## 比較表 — 全{rank_count}製品を一覧できるMarkdown表 (順位 / 製品名 / 価格目安 / 特徴一言)
7. ## まとめ
8. ## よくある質問 (`### Q. 〜` / `A. 〜` 形式厳守) — 3問程度

【視覚的見やすさ (必ず実施)】
- 重要キーワード・結論は `<mark>...</mark>` で黄色ハイライト (各H2で0〜2箇所)
- 価格・スペック・型番は `**太字**` で強調
- 各順位セクションの末尾に `> ポイント:〜` の引用ブロックで要約
- 段落は100〜200文字、だらだら長文NG

【その他ルール】
- 絵文字・記号アクセント禁止。装飾は太字・mark・blockquote・表・箇条書きのみ
- ブロガー個人の経歴・職業など無関係な自己紹介は書かない
- 出力はMarkdownのみ。1行目は「# タイトル」。前置き・コードフェンス不要
- 全体3500〜5500文字
{amazon_section}
{sources_block}
それでは記事を書いてください。
"""


def generate_comparison_article(products, tone_prompt='', genre='ガジェット', amazon_affiliate_tag='', rakuten_affiliate_id='',
                                  extra_instructions='', article_policy='', official_images=None, sources=None,
                                  ownership='not_owned'):
    """products: list of {'name': str, 'image': dict-or-None, 'sources': [source-dict]}
    official_images: list-of-dict (one per product, may include None entries)
    sources: list of additional source dicts (optional)
    """
    persona_intro = f'あなたは経験豊富な日本の{genre}ブロガーです。'
    policy_section = (
        f'【★最優先ルール★】\n{article_policy.strip()}\n\n' if article_policy and article_policy.strip() else ''
    )
    tone_section = f'【口調・テイスト】\n{tone_prompt.strip()}\n\n' if tone_prompt and tone_prompt.strip() else ''
    extra_section = (
        f'\n【ユーザーからの追加指示 (最優先で反映)】\n{extra_instructions.strip()}\n'
        if extra_instructions and extra_instructions.strip() else ''
    )

    products_lines = []
    for i, p in enumerate(products, 1):
        products_lines.append(f'{i}. {p["name"]}')
    products_block = '\n'.join(products_lines)

    title_pattern = ' vs '.join(p['name'] for p in products[:3])

    official_images_section = ''
    if official_images:
        lines = ['\n【各製品の公式画像 — ★必ず使用すること★】']
        for p, img in zip(products, official_images):
            if not img or not img.get('url'):
                continue
            maker = img.get('maker') or '公式サイト'
            lines.append(
                f'- {p["name"]}:\n'
                f'  画像URL: {img["url"]}\n'
                f'  出典: {img.get("page_url","")}\n'
                f'  出典名: {maker}\n'
                f'  → 該当製品の H2セクション直下に `![{p["name"]}]({img["url"]})` と '
                f'`> 引用元: [{maker}]({img.get("page_url","")})` を必ず書く'
            )
        if len(lines) > 1:
            official_images_section = '\n'.join(lines) + '\n'

    sources_block = ''
    if sources:
        parts = []
        for i, s in enumerate(sources):
            parts.append(
                f'■ ソース{i+1}: {s.get("title","")} ({s.get("source","")})\n'
                f'  URL: {s.get("url","")}\n'
                f'  本文:\n{s.get("text","")}'
            )
        sources_block = '【参考情報】\n' + '\n\n'.join(parts) + '\n\n'

    amazon_section = AMAZON_INSTRUCTIONS if amazon_affiliate_tag else ''
    if amazon_affiliate_tag and rakuten_affiliate_id:
        amazon_section += RAKUTEN_INSTRUCTIONS

    prompt = (
        _COMPARISON_PROMPT
        .replace('{persona_intro}', persona_intro)
        .replace('{policy_section}', policy_section)
        .replace('{tone_section}', tone_section)
        .replace('{ownership_section}', _ownership_section(ownership))
        .replace('{extra_section}', extra_section)
        .replace('{products_block}', products_block)
        .replace('{title_pattern}', title_pattern)
        .replace('{official_images_section}', official_images_section)
        .replace('{amazon_section}', amazon_section)
        .replace('{sources_block}', sources_block)
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.8),
    )
    md = (response.text or '').strip()
    if md.startswith('```'):
        md = _strip_code_fence(md)
    title, body = _split_title(md)
    if amazon_affiliate_tag:
        body = _apply_amazon_affiliate(body, amazon_affiliate_tag)
    if rakuten_affiliate_id:
        body = _apply_rakuten_affiliate(body, rakuten_affiliate_id)
    if amazon_affiliate_tag or rakuten_affiliate_id:
        body = '> ※本記事には広告が含まれます。\n\n' + body
    return title, body


def generate_ranking_article(theme, rank_count=5, tone_prompt='', genre='ガジェット',
                              amazon_affiliate_tag='', rakuten_affiliate_id='', extra_instructions='', article_policy='', sources=None,
                              ownership='not_owned'):
    persona_intro = f'あなたは経験豊富な日本の{genre}ブロガーです。'
    policy_section = (
        f'【★最優先ルール★】\n{article_policy.strip()}\n\n' if article_policy and article_policy.strip() else ''
    )
    tone_section = f'【口調・テイスト】\n{tone_prompt.strip()}\n\n' if tone_prompt and tone_prompt.strip() else ''
    extra_section = (
        f'\n【ユーザーからの追加指示 (最優先で反映)】\n{extra_instructions.strip()}\n'
        if extra_instructions and extra_instructions.strip() else ''
    )
    sources_block = ''
    if sources:
        parts = []
        for i, s in enumerate(sources):
            parts.append(
                f'■ ソース{i+1}: {s.get("title","")} ({s.get("source","")})\n'
                f'  URL: {s.get("url","")}\n'
                f'  本文:\n{s.get("text","")}'
            )
        sources_block = '【参考情報】\n' + '\n\n'.join(parts) + '\n\n'

    amazon_section = AMAZON_INSTRUCTIONS if amazon_affiliate_tag else ''
    if amazon_affiliate_tag and rakuten_affiliate_id:
        amazon_section += RAKUTEN_INSTRUCTIONS

    prompt = (
        _RANKING_PROMPT
        .replace('{persona_intro}', persona_intro)
        .replace('{policy_section}', policy_section)
        .replace('{tone_section}', tone_section)
        .replace('{ownership_section}', _ownership_section(ownership))
        .replace('{extra_section}', extra_section)
        .replace('{theme}', theme)
        .replace('{rank_count}', str(rank_count))
        .replace('{amazon_section}', amazon_section)
        .replace('{sources_block}', sources_block)
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.85),
    )
    md = (response.text or '').strip()
    if md.startswith('```'):
        md = _strip_code_fence(md)
    title, body = _split_title(md)
    if amazon_affiliate_tag:
        body = _apply_amazon_affiliate(body, amazon_affiliate_tag)
    if rakuten_affiliate_id:
        body = _apply_rakuten_affiliate(body, rakuten_affiliate_id)
    if amazon_affiliate_tag or rakuten_affiliate_id:
        body = '> ※本記事には広告が含まれます。\n\n' + body
    return title, body


_PILLARIZE_PROMPT = """以下のブログ記事を「ネット上で最も読む価値がある1本」級に大幅加筆してください。
このブログは1日1本だけ投稿する高品質ポリシー。手を抜けない:

【現状】
- 文字数: {char_count}字
- タイトル: {title}

【目標】
- 文字数: **8500〜11000字** (短い場合は2倍近く加筆)
- ピラー記事(網羅型・長期的に PV を稼ぐ看板記事)レベルの厚みと深さ
- 同テーマでGoogle検索上位を取れる情報密度

【加筆前の内側考察 (出力には書かない、頭の中で決める)】
- この記事のターゲット読者は誰か (具体的なペルソナ1〜2文で確定)
- その読者が検索しそうなクエリ (2〜3個想定)
- 上位記事が必ず書く一般論を超える「独自視点」を1〜2個

【加筆方針】
- **既存の内容・データは原則そのまま残す**(削除や言い換えは最小限)
- 新規セクションを 5〜8個 追加して大幅に厚みを増す
- 追加すべき内容(トピックに合わせて関連あるものを選ぶ、全部入れる必要はない):
  - **競合との徹底比較表**(類似製品・サービスを2〜4個出して表で比較)
  - **過去モデル / 旧バージョン との進化点を表で比較**
  - **ユースケース別の活用シナリオ**(ビジネス用 / 個人用 / 学生用 等、各々2〜4行ずつ)
  - **購入を見送るべき人・買うべき人の判断表**(向き不向きを観点別に)
  - **長期的な観点**(2027〜2028年に向けてこの分野はどう進化するか、3〜5項目)
  - **専門用語の解説**(初心者にも分かるように)
  - **歴史的背景**(このトピックがどう進化してきたか)
  - **関連する他社・他サービスとのエコシステム**
- 各新規セクションに **表 or 箇条書き or 引用ブロック** を1つ以上含める
- **比較表は最低3個** (スペック比較・選び方の観点別・用途別おすすめ等、情報密度UP)
- **数字・スペック・固有名詞を最低80箇所以上** 散布(価格・サイズ・容量・mAh・年号・%・回数)
- **権威性外部リンクを3本以上**(メーカー公式・業界統計・Wikipedia等)
- **独自視点セクション** を1個以上必ず追加(上位記事と差別化)
- 「## 」見出しは内容を具体的に示すもの(汎用ラベル禁止、製品名や具体内容を含む)
- マーカー `<mark>...</mark>` を8〜12箇所、太字 `**...**` を20〜30箇所
- 区切り線 `---` を3〜4個入れる
- FAQ は **5問以上** に強化 (FAQPage schema 強化目的)

【絶対遵守】
- 元記事の構造化マークアップ(`[:contents]` `<meta>` 等)があれば維持
- 画像 Markdown(`![]()`)があれば維持
- **`[PRODUCT_CARD: 商品名]` の特殊プレースホルダーは絶対に削除・改変しない (位置もそのまま維持)**
- アフィリエイト関連リンク・出典リンクは維持
- 文中の事実(発表日・スペック数値等)は捏造禁止、不明なら「公式情報待ち」「リーク段階」と明記
- 絵文字・アイコン記号は使わない
- 末尾の「## まとめ」「## よくある質問」は維持(FAQ は `### Q. 〜` `A. 〜` 形式厳守)
- 「私の期待度スコア」セクションが元記事にあれば維持、無ければ追加不要

【元記事】
{body}

【出力】
記事全文の Markdown のみ。前置き・コードフェンス・「以下が加筆版です」のような説明は一切禁止。
1行目は「# タイトル」から始める。"""


def pillarize_article(title, body, max_retries=2):
    """既存の記事を Gemini で「ピラー記事」級に加筆する。

    記事生成パイプラインの2パス目として、初稿(3500〜5500字)を
    5500〜7500字級の網羅型記事に膨らませる。

    失敗時は元の body を返す(ブロックしない)。
    """
    if not (title and body):
        return body
    char_count = len(body)
    # 既に十分長い場合はスキップ (閾値を 5500 → 8000 に引き上げ・1日1本ポリシー)
    if char_count >= 8000:
        return body

    prompt = _PILLARIZE_PROMPT.format(
        char_count=char_count,
        title=title,
        body=body,
    )

    for attempt in range(max_retries):
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.6),
            )
            text = (resp.text or '').strip()
        except Exception as e:
            print(f'[pillarize] gemini attempt {attempt+1} failed: {e}', flush=True)
            continue

        if text.startswith('```'):
            text = _strip_code_fence(text)

        # タイトル行を除いた本文を取得
        new_title, new_body = _split_title(text)

        # 元より短かったら失敗扱い(加筆になっていない)
        if len(new_body) < char_count * 1.2:
            print(f'[pillarize] attempt {attempt+1} too short ({len(new_body)} < {char_count*1.2:.0f}), retry', flush=True)
            continue

        print(f'[pillarize] OK: {char_count} -> {len(new_body)} chars (+{len(new_body)-char_count})', flush=True)
        return new_body

    print(f'[pillarize] all attempts failed, returning original body', flush=True)
    return body


def revise_article(title, body, instructions, tone_prompt='', article_policy='', ownership='unspecified'):
    """既存の記事に対する修正プロンプトを受けて、Geminiに書き直させる。

    戻り値: (new_title, new_body)
    """
    policy_section = ''
    if article_policy and article_policy.strip():
        policy_section = (
            '【★最優先ルール★ — 以下の全指示より優先される。必ず守ること】\n'
            f'{article_policy.strip()}\n\n'
        )

    tone_section = ''
    if tone_prompt and tone_prompt.strip():
        tone_section = f'【口調・テイスト】\n{tone_prompt.strip()}\n\n'

    prompt = (
        f'{policy_section}{tone_section}{_ownership_section(ownership)}'
        '以下のはてなブログ記事を、ユーザーの修正指示に従って書き直してください。\n\n'
        '【ルール】\n'
        '- 修正指示で言及されない部分は基本的に維持する（不要な改変はしない）\n'
        '- Markdown形式を維持する。1行目は「# タイトル」\n'
        '- 既存の画像・引用・[CHART...]マーカー・Amazonリンクは原則そのまま残す（指示で削除/変更を求められた場合のみ変更）\n'
        '- 絵文字は使わない\n'
        '- 出力は記事Markdownのみ。前置き・コードフェンス・コメント不要\n'
        '- ブロガー個人の経歴・職業・別ジャンル（プログラミング言語、別業界の仕事など）の話題は書き出さない\n\n'
        '【ユーザーからの修正指示】\n'
        f'{instructions.strip()}\n\n'
        '【現在の記事】\n'
        f'# {title}\n\n{body}\n\n'
        'それでは修正版の記事を出力してください。'
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.6),
    )
    md = (response.text or '').strip()
    if md.startswith('```'):
        md = _strip_code_fence(md)

    new_title, new_body = _split_title(md)
    if new_title == 'タイトル未設定':
        new_title = title
    return new_title, new_body


def _generate_with_search(client, prompt):
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.85),
    )
    return response.text.strip()


_CHART_RE = re.compile(
    r'\[CHART\s+type="(\w+)"\s+title="([^"]+)"\s+labels="([^"]+)"\s+values="([^"]+)"(?:\s+unit="([^"]*)")?\]'
)

_CHART_COLORS = {
    'bar':  '#4e79a7',
    'line': '#e15759',
    'pie':  None,
}

def _convert_chart_markers(text):
    def replace(m):
        chart_type, title, labels_str, values_str, unit = m.groups()
        unit = (unit or '').strip()
        labels = [l.strip() for l in labels_str.split(',')]
        try:
            values = [float(v.strip()) for v in values_str.split(',')]
        except ValueError:
            return m.group(0)
        if len(labels) != len(values):
            return m.group(0)

        color = _CHART_COLORS.get(chart_type, '#4e79a7')
        dataset = {'label': title, 'data': values}
        if color:
            dataset['backgroundColor'] = color
            dataset['borderColor'] = color

        display_title = f'{title} (単位: {unit})' if unit and chart_type == 'pie' else title

        options = {
            'plugins': {'title': {'display': True, 'text': display_title}},
            'legend': {'display': chart_type == 'pie'},
        }
        if unit and chart_type != 'pie':
            options['scales'] = {
                'y': {'title': {'display': True, 'text': unit}}
            }

        config = {
            'type': chart_type,
            'data': {'labels': labels, 'datasets': [dataset]},
            'options': options,
        }
        encoded = urllib.parse.quote(json.dumps(config, ensure_ascii=False), safe='')
        url = f'https://quickchart.io/chart?c={encoded}&w=600&h=300&bkg=white'
        return f'![{display_title}]({url})'

    return _CHART_RE.sub(replace, text)


_AMAZON_URL_RE = re.compile(r'(https?://(?:www\.|m\.)?amazon\.co\.jp/[^\s\)\]]+)')


def _apply_amazon_affiliate(markdown, tag):
    """Append &tag=XXX to all amazon.co.jp URLs that don't already have a tag."""
    def replace(match):
        url = match.group(1)
        if re.search(r'[?&]tag=', url):
            return url
        sep = '&' if '?' in url else '?'
        return f'{url}{sep}tag={tag}'
    return _AMAZON_URL_RE.sub(replace, markdown)


# 楽天市場 URL を 楽天アフィリエイトの redirect で包む
_RAKUTEN_URL_RE = re.compile(
    r'(https?://(?:www\.|search\.|item\.|directory\.|event\.|books\.)?rakuten\.co\.jp/[^\s\)\]]+)'
)


def _apply_rakuten_affiliate(markdown, aff_id):
    """rakuten.co.jp URL を楽天アフィリエイトの hgc redirect に置換する。

    既に hb.afl.rakuten.co.jp 経由 (= 楽天アフィリエイトリンク) は触らない。
    """
    if not aff_id:
        return markdown

    def replace(match):
        url = match.group(1)
        if 'hb.afl.rakuten.co.jp' in url:
            return url
        wrapped = (
            f'https://hb.afl.rakuten.co.jp/hgc/{aff_id}/'
            f'?pc={urllib.parse.quote(url, safe="")}&link_type=text'
        )
        return wrapped

    return _RAKUTEN_URL_RE.sub(replace, markdown)


def _strip_code_fence(md):
    lines = md.splitlines()
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines)


_TITLE_NG_WORDS = ('徹底解説', '徹底比較', '徹底調査', '徹底', '完全ガイド', '完全攻略',
                   '2026年版', '2026最新', '最新版', '最新', '永久保存')


def _sanitize_title(title):
    """タイトルから v2 禁止語を機械的に除去 + 30文字以内に切り詰め。"""
    t = (title or '').strip()
    # 禁止語を空文字置換 (長い順に処理して部分マッチを避ける)
    for ng in sorted(_TITLE_NG_WORDS, key=lambda x: -len(x)):
        t = t.replace(ng, '')
    # 連続空白・記号を整理
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'[!!]{2,}', '', t).strip()
    t = re.sub(r'[??]{2,}', '?', t).strip()
    # 空のカッコ (sanitize で禁止語が抜けて孤立した括弧) を削除
    t = re.sub(r'【\s*】|\[\s*\]|\(\s*\)|（\s*）', '', t).strip()
    # 末尾のゴミ記号削除
    t = t.rstrip('!?!?。、:|｜・')
    # 区切り記号 (｜・|) で始まる場合は除去
    t = t.lstrip('｜・|: ')
    # 30文字に切り詰め (区切り文字 ｜・| などで切れる場所優先)
    if len(t) > 30:
        # 区切りでの自然なカット
        for sep in ('｜', '|', '、', ',', '。'):
            if sep in t[:30]:
                idx = t.rfind(sep, 0, 30)
                if idx >= 20:  # 区切りで切るのが極端に短くならない場合
                    t = t[:idx].rstrip()
                    break
        if len(t) > 30:
            t = t[:30].rstrip()
    return t or 'タイトル未設定'


_INTENT_LINE_RE = re.compile(r'^\s*検索意図\s*[::]\s*(?:Buy|Know|Do|Go).*$', re.MULTILINE)


def _strip_intent_line(body):
    """v2 プロンプトが出力する『検索意図: Buy』等の内部判定行を本文から削除する。
    判定情報自体は SEO 戦略上は重要だが、公開記事には不要なので除去。
    """
    if not body:
        return body
    body = _INTENT_LINE_RE.sub('', body)
    # 連続空行を1つに圧縮 (検索意図行削除で空行が残ることがある)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    return body


def _split_title(md):
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('# '):
            title = _sanitize_title(line[2:])
            body = '\n'.join(lines[i+1:]).strip()
            body = _strip_intent_line(body)
            return title, body
    return 'タイトル未設定', _strip_intent_line(md)
