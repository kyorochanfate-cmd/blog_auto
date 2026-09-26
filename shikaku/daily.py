"""毎日の自動運用 (GitHub Actions から実行)。

  1. 承認待ちシートで「承認」された更新案を quals.json に反映
  2. 公式ページの変更を確認し、新しい更新案を承認待ちシートに追加
  3. サイトをビルド (残り日数・申込受付状況が日付に合わせて更新される)

デプロイ (Cloudflare Pages への公開) はワークフロー側で行う。

使い方:
  python -m shikaku.daily --state watch_state.json --out shikaku/dist
環境変数: SHIKAKU_REVIEW_SHEET_ID (無ければ 1・2 のシート連携を飛ばす),
          GEMINI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from . import apply, build, review_sheet, watch
from .validate import validate

DATA = Path(__file__).parent / 'data' / 'quals.json'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', default='watch_state.json')
    ap.add_argument('--out', default=str(Path(__file__).parent / 'dist'))
    ap.add_argument('--skip-watch', action='store_true')
    a = ap.parse_args()
    sid = os.environ.get('SHIKAKU_REVIEW_SHEET_ID', '').strip()

    # 1. 承認済みの反映
    if sid:
        approved = review_sheet.pull_approved(sid)
        if approved:
            lines = apply.apply_list(DATA, [p for _, p in approved])
            review_sheet.mark_applied(sid, [n for n, _ in approved])
            for line in lines:
                print('反映:', line)
        else:
            print('反映: 承認済みの更新案なし')

    # 2. 公式ページの変更確認
    if not a.skip_watch:
        report = watch.run(DATA, Path(a.state), Path('proposals.json'))
        print(f'確認: {report["checked"]}件 / 変更あり {len(report["changed"])}件 / '
              f'更新案 {len(report["proposals"])}件 / 却下 {len(report["rejected"])}件 / '
              f'取得失敗 {len(report["errors"])}件')
        for slug, e in report['errors'].items():
            print(f'  取得失敗 {slug}: {e}')
        if sid and report['proposals']:
            names = {q['slug']: q['name'] for q in json.loads(DATA.read_text(encoding='utf-8'))}
            n = review_sheet.push(sid, report['proposals'], names)
            print(f'承認待ちシートに{n}件追加')

    # 3. ビルド
    errs = validate(json.loads(DATA.read_text(encoding='utf-8')))
    if errs:
        raise SystemExit('データ検証エラー:\n' + '\n'.join(errs))
    r = build.build(DATA, Path(a.out), date.today())
    print(f'ビルド: 検索対象 {len(r["indexed"])}ページ / noindex {len(r["noindex"])}ページ')


if __name__ == '__main__':
    main()
