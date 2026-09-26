"""承認された更新案を quals.json に反映する。

proposals.json の各提案に "approved": true が付いたものだけを反映する。
(承認の操作は、スプレッドシート等で人間が行う前提。)

使い方: python -m shikaku.apply --data shikaku/data/quals.json --proposals proposals.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .validate import validate

SCALAR_FIELDS = {'fee_yen', 'fee_note', 'schedule_text', 'eligibility'}


def _merge_exams(cur: list[dict], new: list[dict]) -> list[dict]:
    by_key = {(e.get('exam_date'), e.get('label', '')): e for e in cur}
    for e in new:
        key = (e.get('exam_date'), e.get('label', ''))
        by_key[key] = {**by_key.get(key, {}), **e}
    return sorted(by_key.values(), key=lambda e: e.get('exam_date') or '')


def _merge_rates(cur: list[dict], new: list[dict]) -> list[dict]:
    out = list(cur)
    labels = {r.get('label') for r in out}
    for r in new:
        if r.get('label') in labels:
            out = [({**x, **r} if x.get('label') == r.get('label') else x) for x in out]
        else:
            out.append(r)  # 新しい回は末尾 (= 最新) に追加
    return out


def apply_one(q: dict, p: dict) -> None:
    field, value = p['field'], p.get('value')
    if field in SCALAR_FIELDS:
        q[field] = value
    elif field == 'exams':
        q['exams'] = _merge_exams(q.get('exams') or [], value if isinstance(value, list) else [value])
    elif field == 'pass_rates':
        q['pass_rates'] = _merge_rates(q.get('pass_rates') or [], value if isinstance(value, list) else [value])
    elif field == 'notices':
        items = value if isinstance(value, list) else [value]
        q['notices'] = list(dict.fromkeys((q.get('notices') or []) + items))
    else:
        raise ValueError(f'未対応の項目: {field}')
    if p.get('url') and p['url'] not in (q.get('sources') or []):
        q.setdefault('sources', []).append(p['url'])
    q['checked_at'] = date.today().isoformat()


def run(data_path: Path, proposals_path: Path) -> list[str]:
    report = json.loads(proposals_path.read_text(encoding='utf-8'))
    return apply_list(data_path, report.get('proposals', []))


def apply_list(data_path: Path, proposals: list[dict]) -> list[str]:
    quals = json.loads(data_path.read_text(encoding='utf-8'))
    by_slug = {q['slug']: q for q in quals}
    applied = []
    for p in proposals:
        if not p.get('approved') or not p.get('field'):
            continue
        q = by_slug.get(p['slug'])
        if not q:
            continue
        apply_one(q, p)
        applied.append(f'{p["slug"]}: {p.get("summary") or p["field"]}')
    errs = validate(quals)
    if errs:
        # 反映後のデータが壊れるなら書き込まない
        raise SystemExit('反映するとデータが不正になるため中止:\n' + '\n'.join(errs))
    data_path.write_text(json.dumps(quals, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return applied


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(Path(__file__).parent / 'data' / 'quals.json'))
    ap.add_argument('--proposals', default='proposals.json')
    a = ap.parse_args()
    for line in run(Path(a.data), Path(a.proposals)):
        print('applied:', line)


if __name__ == '__main__':
    main()
