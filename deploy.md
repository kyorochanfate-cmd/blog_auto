# Cloud Run デプロイ手順 (Windows)

## 0. 前提

- Google アカウント (Gmail があればOK)
- 課金が有効化された Google Cloud プロジェクト
  ※ 本ツールの使用量は Cloud Run/Cloud Build とも無料枠内で収まる見込みですが、登録時にクレジットカード入力が必要です。

## 1. gcloud CLI のインストール

https://cloud.google.com/sdk/docs/install から `GoogleCloudSDKInstaller.exe` をダウンロード&実行。

完了後 PowerShell を開き直して確認:

```powershell
gcloud --version
```

## 2. ログイン & プロジェクト作成

```powershell
gcloud auth login
```
ブラウザが開くのでログイン。

新規プロジェクトを作る場合 (PROJECT_ID は世界で一意な英小数字、例: `gadget-blog-tako`):
```powershell
gcloud projects create gadget-blog-tako --name="Gadget Blog"
gcloud config set project gadget-blog-tako
```

既存プロジェクトを使う場合:
```powershell
gcloud config set project YOUR_PROJECT_ID
```

課金紐付け: https://console.cloud.google.com/billing で「請求先アカウント」を当該プロジェクトに紐付け。

## 3. 必要な API を有効化

```powershell
gcloud services enable run.googleapis.com cloudbuild.googleapis.com firestore.googleapis.com
```

## 3.5. Firestore データベースを作成 (ロック状態の保存先)

```powershell
gcloud firestore databases create --location=asia-northeast1
```
※ 既にプロジェクトで Firestore Native モードを使っていればスキップ可能。

Cloud Run のデフォルトサービスアカウントに Firestore 書き込み権限を付与:

```powershell
$PROJECT_NUMBER = gcloud projects describe (gcloud config get-value project) --format="value(projectNumber)"
gcloud projects add-iam-policy-binding (gcloud config get-value project) `
  --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" `
  --role="roles/datastore.user"
```

## 4. パスワード & 署名鍵を生成

PowerShell で:
```powershell
python -c "import secrets; print('FLASK_SECRET_KEY=' + secrets.token_urlsafe(32))"
```
出力された値を控えておきます。

`APP_PASSWORD` は自分で決めた強めのパスワードを用意してください。

## 5. デプロイ

プロジェクトルート (`C:\Users\kyoro\Desktop\ガジェットブログ自動更新`) で:

```powershell
gcloud run deploy gadget-blog `
  --source . `
  --region asia-northeast1 `
  --allow-unauthenticated `
  --memory 512Mi `
  --timeout 600 `
  --cpu-boost `
  --set-env-vars "GEMINI_API_KEY=AIzaSyCe0Ao3d1m-42ey3DtJZrjUqrnwopnlJfQ,GEMINI_MODEL=gemini-3.1-flash-lite-preview,HATENA_ID=tako-chan,HATENA_API_KEY=osokt5ar96,HATENA_BLOG_DOMAIN=tako-karamaru.hatenablog.com,APP_PASSWORD=YOUR_PASSWORD,FLASK_SECRET_KEY=YOUR_RANDOM_STRING,GMAIL_ADDRESS=kyorochan.fate@gmail.com,GMAIL_APP_PASSWORD=yvqh lqnc ibqe lafw"
```

> **`--cpu-boost`**: コールドスタート時の起動CPUを2倍にブースト。スケールゼロから戻る時間が約50%短縮される (無料機能)。

> `YOUR_PASSWORD` `YOUR_RANDOM_STRING` を上で用意した値で置き換えてください。

数分後、`Service URL: https://gadget-blog-xxxxxx-an.a.run.app` のような URL が表示されます。

## 6. スマホで使う

1. 表示された URL をスマホで開く
2. パスワード入力 → ログイン
3. ブラウザのメニューから「ホーム画面に追加」を選択 → アイコンとして常駐

## 7. 再デプロイ (コード更新後)

```powershell
gcloud run deploy gadget-blog --source . --region asia-northeast1
```
※環境変数を変える時のみ `--set-env-vars` を再指定。

## 8. ログを見る (デバッグ用)

```powershell
gcloud run services logs tail gadget-blog --region asia-northeast1
```

## 9. ローカルで動作確認したい場合

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m flask --app webapp.app run --host 0.0.0.0 --port 8080
```
http://localhost:8080 でアクセス。

## トラブルシューティング

- **デプロイ失敗 "Cloud Build trigger required permission"**: `gcloud auth login` をやり直す。または Cloud Build のサービスアカウントに権限を付与する案内に従う。
- **記事生成で `model not found` エラー**: `.env` (またはCloud Run環境変数) の `GEMINI_MODEL` を `gemini-2.5-flash-lite` 等に変更してリトライ。
- **画像が表示されない**: 一部メディアはホットリンク禁止のため。気になる場合は記事編集画面で削除を。
- **はてな投稿で 401 エラー**: `HATENA_API_KEY` が古い可能性。はてなブログ詳細設定で再生成。
