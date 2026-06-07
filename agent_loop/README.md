# agent_loop — Hatena 自律型エージェントループ

Claude Pro / Gemini Advanced の Web UI（Routines / Scheduled Actions）と
Google スプレッドシートを介してバトンを繋ぐ自律ループの Python 側実装。
Python は **A（GA4 → Sheets 追記）** と **D（Sheets → Hatena 投稿）** のみ担当。

## データフロー

```
[A] GA4 ─► analytics_hub.py ─► ①現状データ シート
                                    │
                                    ▼
                       [B] Claude Pro (Web UI Routines)
                       読者傾向を分析 → ②新テイスト方針指示書 シート更新
                                    │
                                    ▼
                       [C] Gemini Advanced (Web UI Scheduled Actions)
                       方針に従い記事を執筆 → ③投稿待ち記事 シートに追加
                                    │
                                    ▼
[D] hatena_publisher.py ◄────── ③投稿待ち記事 シート
        │
        ▼  Hatena AtomPub
   ブログに公開 / 下書き保存
```

## シート構成（同一 SPREADSHEET_ID 内の3タブ）

| タブ名 | 列 | 書き手 | 読み手 |
| --- | --- | --- | --- |
| ①現状データ | ts / date / page_path / page_title / views / avg_engagement_sec / gemini_summary | A (Python) | B (Claude Pro) |
| ②新テイスト方針指示書 | （自由形式） | B (Claude Pro) | C (Gemini Adv.) |
| ③投稿待ち記事 | id / created_at / title / body_md / status / hatena_url / posted_at / error | C (Gemini Adv.) | D (Python) |

ヘッダ行と必要タブは `sheets.ensure_headers()` が初回実行時に自動作成する。

## 環境変数

```
# Hatena AtomPub
HATENA_USER_ID=your_hatena_id
HATENA_BLOG_ID=your_blog_id.hatenablog.com
HATENA_API_KEY=your_atompub_api_key
HATENA_DRAFT=0                       # 1 で下書き保存

# Gemini (整形サマリ用)
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-lite-preview

# Sheets / GA4
GOOGLE_APPLICATION_CREDENTIALS=google_creds.json
SPREADSHEET_ID=your_spreadsheet_id
GA4_PROPERTY_ID=123456789
GA4_LOOKBACK_DAYS=7
```

サービスアカウントには以下を付与し、対象スプレッドシートにも編集権限で共有しておく:
- GA4 プロパティ閲覧権限（Google Analytics 側で `xxx@xxx.iam.gserviceaccount.com` を追加）
- Google Sheets API 有効化

## 実行

```
python -m agent_loop.main --fetch-data    # A: GA4 を集計してシート追記
python -m agent_loop.main --publish       # D: シートから Hatena 投稿
python -m agent_loop.main --all           # A → D を一気に
```

## GitHub Actions による定期実行（無料枠）

`.github/workflows/agent-loop.yml` 参照。
secrets に `.env` の中身（GEMINI_API_KEY / HATENA_*  / SPREADSHEET_ID / GA4_PROPERTY_ID / GCP_SA_KEY）を登録すれば、
朝晩で `--fetch-data` / `--publish` を分けて回せる。
