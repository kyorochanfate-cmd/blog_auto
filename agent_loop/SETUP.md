# agent_loop セットアップ手順（コピペだけで完了）

上から順にやれば終わります。考えなくていいです。

---

## 0. 事前にメモするもの（3つ）

セットアップ中に何度も出てくるので、最初に控えておきます。

| 名前 | どこで取る | 例 |
|---|---|---|
| GCP プロジェクトID | https://console.cloud.google.com/ の上部プロジェクト名横 | `blog-auto-tool-87676` |
| GA4 プロパティID | GA4 → 管理 → プロパティ設定 → 「プロパティID」 | `123456789`（数字9桁） |
| スプレッドシートID | 対象シートURL `docs.google.com/spreadsheets/d/【ここ】/edit` | `1AbC...XyZ` |

---

## 1. GCP 側セットアップ（ターミナルにコピペ）

`gcloud` が入っていない場合は https://cloud.google.com/sdk/docs/install から。
**変数3つ**だけ自分の値に書き換えて、あとは全部そのまま貼ってください。

```bash
# ★ ここだけ書き換え ★
export PROJECT_ID="blog-auto-tool-87676"
export GA4_PROPERTY_ID="123456789"
export SPREADSHEET_ID="1AbC...XyZ"

# 以下コピペでOK
export SA_NAME="agent-loop-sa"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

# 必要APIを有効化
gcloud services enable \
  sheets.googleapis.com \
  analyticsdata.googleapis.com \
  generativelanguage.googleapis.com

# サービスアカウント作成
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Agent Loop (GA4 + Sheets + Hatena)" || true

# JSON鍵を発行（カレントディレクトリに google_creds.json）
gcloud iam service-accounts keys create google_creds.json \
  --iam-account="$SA_EMAIL"

echo
echo "==========================================="
echo "✅ サービスアカウント作成完了"
echo "サービスアカウントのメールアドレス（次のステップで使う）:"
echo "  $SA_EMAIL"
echo "==========================================="
```

実行が終わったら、表示されたメールアドレス（`agent-loop-sa@...iam.gserviceaccount.com`）を**コピー**します。

---

## 2. GA4 にサービスアカウントを追加（ブラウザ操作・2分）

1. https://analytics.google.com/ を開く
2. 左下の **管理（歯車）** → 該当プロパティ → **プロパティのアクセス管理**
3. 右上 **＋ → ユーザーを追加**
4. メール欄に **手順1で表示されたSAアドレス** を貼り付け
5. 役割は **「閲覧者」** を選択
6. **「新規ユーザーにメールで通知する」のチェックは外す**（送れないので）
7. **追加**

---

## 3. スプレッドシートにサービスアカウントを共有（30秒）

1. 対象のスプレッドシートを開く
2. 右上 **共有** ボタン
3. 同じSAアドレスを貼り付け、権限は **編集者**
4. **「通知を送信」のチェックは外す**
5. **共有**

---

## 4. Gemini API キー取得（30秒）

1. https://aistudio.google.com/apikey を開く
2. 「Create API key」→ GCPプロジェクト（手順1の `PROJECT_ID`）を選択
3. 表示された `AIza...` のキーをコピー

---

## 5. はてなブログ AtomPub キー取得（30秒）

1. はてなブログにログイン
2. ダッシュボード → **設定** → **詳細設定**
3. ページ下部 **AtomPub** 欄に「ルートエンドポイント」と「APIキー」がある
4. **APIキー**（英数字の長い文字列）をコピー
5. ついでに **はてなID**（例: `kyorochan-fate`）と **ブログID**（例: `kyorochan-fate.hatenablog.com`）も控える

---

## 6. ローカル疎通テスト（GitHub Actions の前に1回ローカルで通す）

リポジトリのルートで:

```bash
# 依存インストール
pip install -r requirements.txt

# .env を作成（★の5箇所を自分の値に書き換え）
cat > .env <<'EOF'
GEMINI_API_KEY=★ステップ4のキー
GEMINI_MODEL=gemini-3.1-flash-lite-preview

HATENA_USER_ID=★はてなID
HATENA_BLOG_ID=★blog_id.hatenablog.com
HATENA_API_KEY=★ステップ5のAPIキー
HATENA_DRAFT=1

GOOGLE_APPLICATION_CREDENTIALS=google_creds.json
SPREADSHEET_ID=★スプレッドシートID
GA4_PROPERTY_ID=★GA4プロパティID
GA4_LOOKBACK_DAYS=7

# webapp 側 (agent_loop では使わないがダミーでOK)
GOOGLE_OAUTH_CLIENT_ID=dummy
ALLOWED_EMAILS=dummy@example.com
EOF

# 集計テスト（GA4 → Sheets）
python -m agent_loop.main --fetch-data -v
```

→ スプレッドシートに `①現状データ` タブができ、GA4データが入っていれば成功。

**重要**: `HATENA_DRAFT=1` にしているので、最初は下書き保存になります。
動作確認できたら本番運用時に `0` に変えてください。

---

## 7. GitHub Secrets 登録（`gh` でコピペ完了）

`gh auth login` 済みの前提。リポジトリのルートで:

```bash
# ★ 5箇所書き換え ★（他は手順1〜5で取得済みの値）
gh secret set GCP_SA_KEY < google_creds.json
gh secret set SPREADSHEET_ID -b "1AbC...XyZ"
gh secret set GA4_PROPERTY_ID -b "123456789"
gh secret set GEMINI_API_KEY -b "AIza..."
gh secret set HATENA_USER_ID -b "kyorochan-fate"
gh secret set HATENA_BLOG_ID -b "kyorochan-fate.hatenablog.com"
gh secret set HATENA_API_KEY -b "はてなAPIキー"
```

確認:
```bash
gh secret list
```

`.github/workflows/agent-loop.yml` が次の日の 07:00 / 08:00 JST から自動で回り始めます。
すぐ試したいときは:
```bash
gh workflow run agent-loop.yml -f mode=fetch-data
```

---

## 8. Claude Pro Routines に貼るプロンプト

Claude.ai（または Desktop アプリ）の **Routines** で「毎日 07:30」に設定し、以下をそのまま貼ります。

```
あなたは私のはてなブログのエディターです。

以下のGoogleスプレッドシートを開いて、シート「①現状データ」の直近7日分を読んでください:
https://docs.google.com/spreadsheets/d/★SPREADSHEET_ID★/edit

そこから読者傾向を分析してください。観点:
- 滞在時間が長い / 短い記事の特徴
- どんなテーマ・カテゴリのPVが伸びているか
- タイトルの傾向（疑問形 / 数字 / レビュー型 など）

その分析を踏まえ、シート「②新テイスト方針指示書」のA1セルを次の形式で上書きしてください:

---
更新日時: YYYY-MM-DD HH:MM
注目テーマ: （3つ箇条書き）
タイトル方針: （1行）
本文の長さ・テイスト: （1行）
避けるべき要素: （1行）
明日書くべき記事候補（タイトル案を5本）:
  1. ...
  2. ...
  ...
---

注意:
- シート「②新テイスト方針指示書」のA1を「上書き」する（追記ではない）
- 過去の指示は捨てて構わない（最新の傾向のみを反映）
```

`★SPREADSHEET_ID★` を実際のIDに置換してから貼ってください。

---

## 9. Gemini Advanced Scheduled Actions に貼るプロンプト

Gemini Advanced（gemini.google.com）の **Scheduled Actions** で「毎日 06:00」に設定し、以下を貼ります。

```
私のはてなブログ用の記事を1本書いてください。

参照するスプレッドシート:
https://docs.google.com/spreadsheets/d/★SPREADSHEET_ID★/edit

手順:
1. シート「②新テイスト方針指示書」のA1を読み、今日の方針を確認する
2. その方針の「明日書くべき記事候補」から1本を選ぶ（直近7日で似たタイトルがシート③に既にあれば別の候補を選ぶ）
3. ガジェットの深い事実分析（Deep Factual Analysis）をベースに、Markdownで本文を執筆する
   - 2500〜4000字
   - 見出しは ## と ### を使う
   - 結論を最初に、根拠を後段に
   - 推測ではなく仕様・公式情報を優先
4. シート「③投稿待ち記事」の末尾に1行追加する。列の値:
   - A列 id: 「YYYYMMDD-連番」
   - B列 created_at: 今のISO8601時刻
   - C列 title: 記事タイトル
   - D列 body_md: Markdown本文（セル内改行OK）
   - E列 status: 空欄のままにする（Python側が "pending" として処理する）
   - F〜H列: 空欄

注意:
- E列を「posted」「failed」にはしない（Python投稿スクリプトが触る列）
- 1日1本だけ追加する
```

`★SPREADSHEET_ID★` を実際のIDに置換してから貼ってください。

---

## 10. 完了

| 役割 | スケジュール | 担当 |
|---|---|---|
| A: GA4集計 → ①シート | 07:00 JST | GitHub Actions |
| B: 傾向分析 → ②シート | 07:30 JST | Claude Pro Routines |
| C: 記事執筆 → ③シート | 06:00 JST（翌朝用） | Gemini Advanced Scheduled |
| D: ③シート → はてな投稿 | 08:00 JST | GitHub Actions |

> **本番化**: 動作確認後、`.env` と GitHub Actions の `HATENA_DRAFT` を `0` に変えると、下書きではなく即時公開になります。
