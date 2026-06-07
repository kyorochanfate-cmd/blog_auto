# ガジェットブログ自動更新ツール (Webアプリ版)

ガジェット系ニュースから話題のトピックを抽出 → AIが記事生成 → スマホでプレビュー&承認 → はてなブログへ自動投稿。Cloud Run にデプロイしてスマホ単独で運用するためのツール。

## 機能

1. 複数RSSフィード (ITmedia / ASCII / PC Watch / GIZMODO Japan / ガジェット通信) から最新ガジェット記事を収集
2. Gemini API がホット5件を抽出 (重複排除・クラスタリング)
3. ユーザーが選択 → 関連記事の本文と og:image を取得
4. Gemini が独自の文体で記事生成 (パクリ判定回避プロンプト + 個人的所感を必ず含める)
5. 公式画像を1〜2点引用 (出典URLを明記、引用4要件遵守)
6. スマホでプレビュー → 「投稿する」タップで AtomPub によりはてなブログへ公開投稿

## 構成

```
.
├── webapp/
│   ├── app.py                # Flask アプリ本体
│   ├── templates/            # 画面テンプレート
│   └── static/style.css      # スマホ最適化スタイル
├── src/
│   ├── news_collector.py     # RSS 収集 (サムネイル付き)
│   ├── topic_selector.py     # Gemini で5件抽出
│   ├── researcher.py         # 本文 & og:image 抽出
│   ├── article_generator.py  # Gemini で記事生成 (画像引用込み)
│   └── hatena_publisher.py   # AtomPub で投稿
├── feeds.py                  # RSSフィード一覧
├── config.py                 # .env 読み込み
├── Dockerfile                # Cloud Run 用
├── .gcloudignore
└── deploy.md                 # Cloud Run デプロイ手順
```

## セットアップ

### 1. `.env` を編集

`.env` を開き、`APP_PASSWORD` (Webアプリのアクセスパスワード) と `FLASK_SECRET_KEY` (セッション署名鍵) を設定してください。

`FLASK_SECRET_KEY` の生成例:
```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. はてなブログを Markdown モードに

ブログ管理画面 → 設定 → 基本設定 → 「編集モード」を **Markdown モード** に変更。

### 3. Cloud Run へデプロイ

詳細は [deploy.md](deploy.md) を参照。要点:

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
gcloud run deploy gadget-blog --source . --region asia-northeast1 --allow-unauthenticated --memory 512Mi --timeout 600 --set-env-vars "..."
```

## ローカル開発

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m flask --app webapp.app run --port 8080
```

## 著作権配慮について

- 画像はソース記事の og:image を **引用** する形 (再ホストせず、URLを参照)。
- すべての引用画像に出典URLを明記 (Markdown ブロッククォートで `引用元: [サイト名](URL)` を併記)。
- 記事末尾に【参考】セクションで全ソースURLを列挙。
- 本文はGeminiが独自の構成・文体で書き起こし (元記事の文章を直接コピーしない設計)。
- 引用4要件 (必要性・主従関係・明瞭区別性・出所明示) を満たす作りになっていますが、投稿前にプレビューで自分の目で必ず確認してください。

## セキュリティ機能

### ブルートフォース対策
- **3回連続でパスワードを間違えると即時ロック**
- ロック発生時、登録した Gmail アドレス (`GMAIL_ADDRESS`) にロック解除リンク付きメールを自動送信
- リセットリンクは30分間のみ有効、1回のクリックで消費
- 緊急復旧: Cloud Run ログにリセットURLが記録される / Firestore Console で `auth/state` 削除でも即解除可能
- ロック状態は **Firestore** に永続化 (Cloud Run のスケールゼロでも維持)

### その他
- 認証は単一パスワード。あなた専用の私的ツールとしての設計です。第三者と共有しないでください。
- セッション Cookie は `FLASK_SECRET_KEY` で署名。デプロイ後は鍵を変更しない (変更すると全セッション無効化)。

## 注意事項

- Cloud Run のスケールゼロ動作上、初回アクセス時に数秒のコールドスタートがあります。
- Gemini API は使用量課金。1記事生成で数〜数十円程度の見込みですが、AI Studio で利用状況を確認してください。
- Firestore は無料枠 (1日 50K 読み取り / 20K 書き込み) で十分。本ツールの規模では超えません。
