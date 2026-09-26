# 資格カレンダー (shikaku-calendar.com)

資格試験の日程・申込期間・受験料・合格率を、実施団体の公式情報から整理して毎日更新する静的サイト。
収益源は通信講座（資料請求・申込）のアフィリエイト。

## なぜこの形か

- AIに「記事」を量産させる方式は、Google の大量生成コンテンツ対策で評価されない
- 代わりに、プログラムが得意な「公式情報を集めて、正確に、毎日更新し続ける」ことそのものを価値にする
- 本文の数字はすべてデータから組み立てる（AIの作文と数字が食い違わない）
- データが揃っていないページは自動で `noindex` にする（中身の薄いページを検索に出さない）

## 構成

```
shikaku/
  build.py        データ → 静的HTML (dist/)。毎日実行して残り日数・受付状況を更新
  content.py      要約文・FAQ・noindex判定をデータから決定的に生成
  charts.py       合格率推移グラフ (inline SVG, JS不要)
  templates/      Jinja2 テンプレート
  static/         CSS / 勉強計画ツールJS / favicon
  data/quals.json 資格データ (公式情報のみ。出典URLと確認日つき)
```

## ビルド

```bash
python -m shikaku.build                       # shikaku/data/quals.json → shikaku/dist/
python -m shikaku.build --today 2026-09-26    # 日付を固定して検証
```

ビルド結果の最後に、`noindex` になったページとその理由が表示される。

## データの形式 (quals.json の1件)

```json
{
  "slug": "kiken-otsu4",
  "name": "危険物取扱者 乙種第4類",
  "category": "安全衛生",
  "organizer": "一般財団法人 消防試験研究センター",
  "official_url": "https://...",
  "schedule_text": "都道府県ごとに年数回",
  "exam_format": "マークシート",
  "eligibility": "誰でも受験可",
  "fee_yen": 5300,
  "fee_note": "非課税",
  "exams": [{"label": "第1回", "exam_date": "2026-11-15", "apply_start": "2026-09-01", "apply_end": "2026-09-20"}],
  "pass_rates": [{"label": "2024年度", "rate": 31.8, "examinees": 200000}],
  "study_hours": {"min": 40, "max": 60, "source": "出典名"},
  "courses": [{"name": "講座名", "url": "アフィリエイトURL", "note": "特徴"}],
  "sources": ["https://..."],
  "checked_at": "2026-09-26"
}
```

- `pass_rates` は古い順
- 確認できない項目は入れない（推測値は書かない）
- `checked_at` が180日より古いページは自動で `noindex`

## 運用の原則

1. 数字は実施団体の公式発表のみ。出典URLと確認日を必ず残す
2. 公式ページの変更は自動検知するが、反映は人間の承認後
3. 体験談・合格体験記を AI で作らない
4. 広告表記（ステマ規制）はフッターと運営者情報に常時表示
