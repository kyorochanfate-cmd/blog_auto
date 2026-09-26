"""資格カレンダー: データ → 静的サイト (dist/) を生成する。

使い方:
  python -m shikaku.build --data shikaku/data/quals.json --out shikaku/dist
  python -m shikaku.build --today 2026-09-26   # 日付を固定して生成 (検証用)

毎日このビルドを回すことで、試験日までの残り日数・申込受付状況が更新される。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts, content
from .validate import validate

HERE = Path(__file__).resolve().parent

SITE = {
    'name': '資格カレンダー',
    'url': 'https://shikaku-calendar.com',
    'description': '資格試験の日程・申込期間・受験料・合格率の推移を、公式情報をもとに毎日更新しています。',
}

CATEGORY_ORDER = [
    'IT', '会計・金融', '不動産', '医療・介護・福祉', '電気・設備・建設',
    '安全衛生', '事務・ビジネス', '法律・労務', '教育・語学', 'その他',
]


_WEEKDAYS = '月火水木金土日'


def _as_date(d):
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(d)
    except (TypeError, ValueError):
        return None


def _fmt_md(d, today: date) -> str:
    """スマホの表で折り返さない短い表記: 10/18(日)。年が違う時だけ年を付ける。"""
    x = _as_date(d)
    if not x:
        return '?'
    head = '' if x.year == today.year else f'{x.year}/'
    return f'{head}{x.month}/{x.day}({_WEEKDAYS[x.weekday()]})'


def _fmt_ymd(d) -> str:
    x = _as_date(d)
    return f'{x.year}年{x.month}月{x.day}日({_WEEKDAYS[x.weekday()]})' if x else '?'


def load_quals(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    for q in data:
        q.setdefault('category', 'その他')
        q.setdefault('pass_rates', [])
        q.setdefault('exams', [])
        q.setdefault('courses', [])
        q.setdefault('sources', [])
    return data


def build(data_path: Path, out: Path, today: date) -> dict:
    env = Environment(
        loader=FileSystemLoader(HERE / 'templates'),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(site=SITE, today=today)
    env.filters['md'] = lambda d: _fmt_md(d, today)
    env.filters['ymd'] = _fmt_ymd

    quals = load_quals(data_path)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(HERE / 'static', out / 'static')

    pages = []           # sitemap 用 (indexable なものだけ)
    report = {'indexed': [], 'noindex': {}}
    calendar = []        # トップの「近日の試験」一覧用

    for q in quals:
        ups = content.upcoming_exams(q, today)
        indexable, reasons = content.is_indexable(q, today)
        ctx = {
            'q': q,
            'upcoming': ups,
            'stats': content.pass_rate_stats(q),
            'has_rates': content.has_pass_rates(q),
            'multi_series': len(content.rate_series(q)) > 1,
            'has_calc': any(r.get('calc') for r in q['pass_rates']),
            'summary': content.summary_sentences(q, today),
            'faqs': content.faqs(q, today),
            'charts': [(name, charts.pass_rate_svg(rows))
                       for name, rows in content.rate_series(q).items()],
            # 表は系列ごとに、新しい回を上に
            'rate_rows': [r for rows in content.rate_series(q).values() for r in reversed(rows)],
            'indexable': indexable,
            'path': f'/q/{q["slug"]}/',
        }
        html = env.get_template('qual.html').render(**ctx)
        dest = out / 'q' / q['slug']
        dest.mkdir(parents=True)
        (dest / 'index.html').write_text(html, encoding='utf-8')

        if indexable:
            pages.append({'path': ctx['path'], 'lastmod': q.get('checked_at')})
            report['indexed'].append(q['slug'])
        else:
            report['noindex'][q['slug']] = reasons
        for ex in ups:
            calendar.append({'q': q, 'ex': ex})

    calendar.sort(key=lambda c: c['ex']['date'])
    by_cat = {}
    for q in quals:
        by_cat.setdefault(q['category'], []).append(q)
    cats = [(c, by_cat[c]) for c in CATEGORY_ORDER if c in by_cat]
    cats += [(c, v) for c, v in by_cat.items() if c not in CATEGORY_ORDER]

    # 申込締切が近い順 (受付中のもの)
    closing = [c for c in calendar if c['ex']['apply_status'] == 'open']
    closing.sort(key=lambda c: c['ex']['apply_days_left'])

    simple_pages = {
        'index.html': ('index.html', {'calendar': calendar[:30], 'closing': closing[:10],
                                      'cats': cats, 'path': '/'}),
        'tools/plan/index.html': ('plan.html', {'path': '/tools/plan/'}),
        'about/index.html': ('about.html', {'path': '/about/'}),
        'privacy/index.html': ('privacy.html', {'path': '/privacy/'}),
    }
    for rel, (tpl, ctx) in simple_pages.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(tpl).render(indexable=True, **ctx), encoding='utf-8')
        pages.append({'path': ctx['path'], 'lastmod': today.isoformat()})

    # 勉強計画ツール用のデータ (ブラウザ側 JS が読む)
    plan_data = []
    for q in quals:
        ups = content.upcoming_exams(q, today)
        plan_data.append({
            'slug': q['slug'],
            'name': q['name'],
            'exams': [{'label': e.get('label', ''), 'date': e['date'].isoformat()} for e in ups],
            'hours': q.get('study_hours'),
        })
    (out / 'static' / 'plan-data.json').write_text(
        json.dumps(plan_data, ensure_ascii=False), encoding='utf-8')

    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in pages:
        sitemap.append(f'<url><loc>{SITE["url"]}{p["path"]}</loc>'
                       + (f'<lastmod>{p["lastmod"]}</lastmod>' if p['lastmod'] else '')
                       + '</url>')
    sitemap.append('</urlset>')
    (out / 'sitemap.xml').write_text('\n'.join(sitemap), encoding='utf-8')
    (out / 'robots.txt').write_text(
        f'User-agent: *\nAllow: /\nSitemap: {SITE["url"]}/sitemap.xml\n', encoding='utf-8')
    (out / '404.html').write_text(
        env.get_template('404.html').render(indexable=False, path='/404'), encoding='utf-8')
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(HERE / 'data' / 'quals.json'))
    ap.add_argument('--out', default=str(HERE / 'dist'))
    ap.add_argument('--today', default=None, help='YYYY-MM-DD (検証用に日付を固定)')
    a = ap.parse_args()
    today = date.fromisoformat(a.today) if a.today else date.today()
    errs = validate(json.loads(Path(a.data).read_text(encoding='utf-8')))
    if errs:
        print('\n'.join(errs))
        raise SystemExit(f'データに問題があるためビルドを中止しました ({len(errs)}件)')
    report = build(Path(a.data), Path(a.out), today)
    print(f'indexed: {len(report["indexed"])}')
    for slug, reasons in report['noindex'].items():
        print(f'noindex: {slug} ({", ".join(reasons)})')


if __name__ == '__main__':
    main()
