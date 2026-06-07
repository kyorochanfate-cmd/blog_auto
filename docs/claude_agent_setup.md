# Claude Code scheduled agent — 記事ライティング委譲設定

このドキュメントは Cloud Run + Gemini パイプラインの「記事本文執筆」だけを
**Claude Code の scheduled remote agent (Opus 4.7)** に委譲する手順を説明します。

## 構成図

```
[Cloud Scheduler 08:00 JST]
  ↓
[Cloud Run /auto-run/<blog_id>]
  ↓ (use_claude_writing=True なら本文を書かずに pending_writes キューへ)
[Firestore: pending_writes]
  status: 'pending'
  ↓
[Claude Code agent 08:05 JST scheduled]
  ↓ GET /admin/next-pending-write?blog_id=xxx
  ↓ Opus 4.7 で本文執筆
  ↓ POST /admin/submit-written-body/<doc_id> { title, body }
  ↓
[Cloud Run finalize_from_queue]
  ↓ 商品カード/CTA/JSON-LD/画像rehost/Hatena投稿/Threads投稿
[完了]

[保険] [Cloud Scheduler 08:35 JST → /admin/claude-fallback]
  ↓ 30分以上 pending のものは Gemini で書いて公開
```

## セットアップ手順

### 1. ブログに `use_claude_writing=True` フラグを立てる

Cloud Run admin UI または Firestore コンソールで該当ブログのフラグを true に。

```
gcloud firestore documents update blogs/iUJOYsRRRjXk2LFoXW7l \
  --update=use_claude_writing=true \
  --project=blog-auto-tool-87676
```

(Firestore CLI 制約があるので、Web UI から編集の方が手軽)

### 2. Claude Code で scheduled agent を作成

ローカルの Claude Code (CLI または IDE) で以下のように `/schedule` (schedule skill) を使う:

```
/schedule
```

スケジュール内容として以下を指定:

- **頻度**: 毎日 08:05 JST (cron `5 8 * * *`、TZ Asia/Tokyo)
- **エージェントへの指示** (プロンプト): 下記「Agent プロンプト」をそのままコピペ

### 3. Agent プロンプト (この内容をそのまま schedule に登録)

````
あなたは「たこちゃん、今日もガジェットに絡まる」というガジェットブログの専属ライターです。
Cloud Run のキューから今日書くべき記事の指示を取得し、Opus 4.7 の力でピラー級の記事を執筆して送り返してください。

【手順】

1. まずキューから次の記事を取得:
```bash
curl -s -X GET \
  -H "Authorization: Bearer ${AUTO_RUN_TOKEN}" \
  "https://gadget-blog-437023190013.asia-northeast1.run.app/admin/next-pending-write?blog_id=iUJOYsRRRjXk2LFoXW7l"
```

(AUTO_RUN_TOKEN は環境変数 or secret に保存しておく)

2. レスポンス JSON の `pending` が false なら作業終了。終了報告して停止してOK。

3. `pending: true` の場合、レスポンスに含まれる `prompt` フィールドが完全な記事執筆指示です。
   このプロンプトを **そのまま自分への指示** として読み、本文を執筆してください。

4. 執筆ルール:
   - 出力は **Markdown のみ**。1行目は `# タイトル`。前置きやコードフェンスは絶対に書かない
   - **7000〜11000字** のピラー級
   - prompt 内の「品質チェックリスト」を全項目満たす
   - `[PRODUCT_CARD: 商品名]` プレースホルダを最低2個含める
   - `[:contents]` を導入直後に1行入れる
   - FAQ は `### Q. 〜` / `A. 〜` 形式で5問以上

5. 書き終えたら以下の curl で Cloud Run に送り返す:

```bash
curl -s -X POST \
  -H "Authorization: Bearer ${AUTO_RUN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @article.json \
  "https://gadget-blog-437023190013.asia-northeast1.run.app/admin/submit-written-body/${DOC_ID}"
```

ここで `article.json` は `{"title": "記事タイトル", "body": "本文Markdown"}` の JSON。
DOC_ID は手順1で取得した `doc_id`。

6. レスポンスの ok=true を確認して完了。失敗時はエラー内容を確認してリトライ。

【重要】
- 同じ doc_id を二度送らない (重複公開防止)
- 30分以内に submit できない場合、Gemini fallback が走るので焦らない (ただし可能なら自分で完遂)
- 認証トークンは Claude Code の環境変数 / secret 機能から読む
````

### 4. 保険 cron (Gemini fallback) を Cloud Scheduler に追加

```bash
gcloud scheduler jobs create http claude-fallback \
  --location=asia-northeast1 \
  --project=blog-auto-tool-87676 \
  --schedule="35 8 * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="https://gadget-blog-437023190013.asia-northeast1.run.app/admin/claude-fallback?stale=30&limit=3" \
  --http-method=POST \
  --headers="Authorization=Bearer YOUR_AUTO_RUN_TOKEN" \
  --attempt-deadline="600s"
```

`YOUR_AUTO_RUN_TOKEN` は Cloud Run の AUTO_RUN_TOKEN env と同じ値。

これで 08:35 に「30分前(=08:05時点)から書かれていない記事があれば Gemini で代行公開」が走ります。

## 動作確認

1. **キューの状態確認**:
   ```bash
   curl -H "Authorization: Bearer ${TOKEN}" \
     "https://gadget-blog-437023190013.asia-northeast1.run.app/admin/pending-writes-status?blog_id=iUJOYsRRRjXk2LFoXW7l"
   ```
   レスポンス例: `{"pending": 0, "writing": 0, "published": 5, "failed": 0, "fallback_published": 1}`

2. **手動テスト (Claude を使わず agent 動作確認)**:
   - 08:00 の auto-run 後に GET /admin/next-pending-write → doc_id 取得
   - 適当な title/body を POST /admin/submit-written-body/<doc_id>
   - 記事が公開されるか確認

## 運用ルール

- **Pro プランの5h制限**: 1日1本ペースなら問題なし。agent 実行時刻に他の Claude Code 作業をしないこと
- **agent が失敗した場合**: 30分後の fallback で必ず公開される。慌てる必要なし
- **本文質を確認**: 最初の1週間は記事を毎日チェック。Claude の出力品質が期待通りか確認
- **コスト感**: Pro プランの月額 $20 内で完結。Gemini API 料金は変わらず約 ¥630/月
