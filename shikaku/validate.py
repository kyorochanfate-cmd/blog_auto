"""quals.json の整合性チェック。ビルド前に必ず通す。

使い方: python -m shikaku.validate [path]
問題があれば一覧を出して終了コード 1。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

CATEGORIES = {
    'IT', '会計・金融', '不動産', '医療・介護・福祉', '電気・設備・建設',
    '安全衛生', '事務・ビジネス', '法律・労務', '教育・語学', 'その他',
}
SLUG_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
URL_RE = re.compile(r'^https?://\S+$')


def _date(v) -> date | None:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def validate(quals: list[dict]) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    for i, q in enumerate(quals):
        who = q.get('slug') or f'#{i}'
        slug = q.get('slug', '')
        if not SLUG_RE.match(slug):
            errs.append(f'{who}: slug が不正')
        if slug in seen:
            errs.append(f'{who}: slug が重複')
        seen.add(slug)
        if not q.get('name'):
            errs.append(f'{who}: name がない')
        if q.get('category') not in CATEGORIES:
            errs.append(f'{who}: category が不正 ({q.get("category")})')
        for key in ('official_url',):
            if q.get(key) and not URL_RE.match(q[key]):
                errs.append(f'{who}: {key} がURLではない')
        for s in q.get('sources') or []:
            if not URL_RE.match(s):
                errs.append(f'{who}: sources にURLでないもの ({s[:40]})')
        fee = q.get('fee_yen')
        if fee is not None and (not isinstance(fee, int) or not 0 < fee < 200_000):
            errs.append(f'{who}: fee_yen が不正 ({fee})')
        for ex in q.get('exams') or []:
            d = _date(ex.get('exam_date'))
            if not d:
                errs.append(f'{who}: exam_date が不正 ({ex.get("exam_date")})')
                continue
            s, e = ex.get('apply_start'), ex.get('apply_end')
            ds, de = _date(s), _date(e)
            if s and not ds:
                errs.append(f'{who}: apply_start が不正 ({s})')
            if e and not de:
                errs.append(f'{who}: apply_end が不正 ({e})')
            if ds and de and ds > de:
                errs.append(f'{who}: 申込開始が締切より後 ({s} > {e})')
            if de and de > d:
                errs.append(f'{who}: 申込締切が試験日より後 ({e} > {d})')
        rates = q.get('pass_rates') or []
        for r in rates:
            v = r.get('rate')
            if not isinstance(v, (int, float)) or not 0 < v <= 100:
                errs.append(f'{who}: 合格率が不正 ({r})')
            if not r.get('label'):
                errs.append(f'{who}: 合格率にラベルがない')
            ex = r.get('examinees')
            if ex is not None and (not isinstance(ex, int) or ex <= 0):
                errs.append(f'{who}: 受験者数が不正 ({ex})')
        if not _date(q.get('checked_at')):
            errs.append(f'{who}: checked_at が不正')
    return errs


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / 'data' / 'quals.json')
    quals = json.loads(path.read_text(encoding='utf-8'))
    errs = validate(quals)
    if errs:
        print('\n'.join(errs))
        print(f'NG: {len(errs)}件')
        sys.exit(1)
    print(f'OK: {len(quals)}件')


if __name__ == '__main__':
    main()
