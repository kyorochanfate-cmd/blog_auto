"""資格データから、ページに載せる文章・FAQ・判定を組み立てる。

解説文は AI に書かせず、データから決定的に組み立てる。
こうすれば本文中の数字が表・グラフと食い違うことがない。
"""

from __future__ import annotations

from datetime import date, timedelta

# データの確認日がこれより古いページは検索に出さない (情報が古い可能性があるため)
STALE_AFTER_DAYS = 180


def _parse(d: str | None) -> date | None:
    if not d:
        return None
    try:
        return date.fromisoformat(d)
    except ValueError:
        return None


def upcoming_exams(q: dict, today: date) -> list[dict]:
    """今日以降の試験回を日付順に返す。残り日数・申込状況を付与。"""
    out = []
    for ex in q.get('exams') or []:
        d = _parse(ex.get('exam_date'))
        if not d or d < today:
            continue
        a_start = _parse(ex.get('apply_start'))
        a_end = _parse(ex.get('apply_end'))
        if a_start and today < a_start:
            status = 'before'
        elif a_end and today <= a_end:
            status = 'open'
        elif a_end and today > a_end:
            status = 'closed'
        else:
            status = 'unknown'
        out.append({
            **ex,
            'date': d,
            'days_left': (d - today).days,
            'apply_status': status,
            'apply_days_left': (a_end - today).days if a_end and today <= a_end else None,
        })
    out.sort(key=lambda e: e['date'])
    return out


def rate_series(q: dict) -> dict[str, list[dict]]:
    """合格率を系列ごとに分ける (学科/技能、第一次/第二次 など)。
    系列を混ぜて1本のグラフにすると別物の数字を比べてしまうため。
    系列指定が無い資格は '' の1系列になる。並びはデータ順 (古い順) を保つ。"""
    out: dict[str, list[dict]] = {}
    for r in q.get('pass_rates') or []:
        if isinstance(r.get('rate'), (int, float)):
            out.setdefault(r.get('series') or '', []).append(r)
    return out


def _stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    latest = rows[-1]
    rates = [r['rate'] for r in rows]
    stats = {
        'latest': latest,
        'avg': round(sum(rates) / len(rates), 1),
        'min': min(rows, key=lambda r: r['rate']),
        'max': max(rows, key=lambda r: r['rate']),
        'count': len(rows),
    }
    if len(rows) >= 2:
        diff = round(latest['rate'] - rows[-2]['rate'], 1)
        stats['diff_prev'] = diff
    return stats


def pass_rate_stats(q: dict) -> dict | None:
    """単一系列の資格の統計。複数系列の資格では None (系列別は series_stats を使う)。"""
    series = rate_series(q)
    if len(series) != 1:
        return None
    return _stats(next(iter(series.values())))


def series_stats(q: dict) -> list[tuple[str, dict]]:
    return [(name, _stats(rows)) for name, rows in rate_series(q).items()]


def has_pass_rates(q: dict) -> bool:
    return bool(rate_series(q))


def _latest_line(q: dict) -> str | None:
    """「直近の合格率は…」の一文。複数系列なら同じ回の数字を並べる。"""
    ss = series_stats(q)
    if not ss:
        return None
    if len(ss) == 1:
        st = ss[0][1]
        lt = st['latest']
        line = f'直近（{lt["label"]}）の合格率は{lt["rate"]}%'
        if st['count'] >= 3:
            return line + f'で、過去{st["count"]}回の平均は{st["avg"]}%です。'
        return line + 'です。'
    labels = {st['latest']['label'] for _, st in ss}
    if len(labels) == 1:
        parts = [f'{name}{st["latest"]["rate"]}%' for name, st in ss]
        return f'直近（{labels.pop()}）の合格率は、' + '・'.join(parts) + 'です。'
    parts = [f'{name}{st["latest"]["rate"]}%（{st["latest"]["label"]}）' for name, st in ss]
    return '直近の合格率は、' + '・'.join(parts) + 'です。'


def summary_sentences(q: dict, today: date) -> list[str]:
    """ページ冒頭の要約。すべてデータ由来。"""
    s = []
    name = q['name']
    ups = upcoming_exams(q, today)
    if ups:
        nx = ups[0]
        s.append(f'次回の{name}は{nx["date"].year}年{nx["date"].month}月{nx["date"].day}日'
                 f'（あと{nx["days_left"]}日）です。')
        if nx['apply_status'] == 'open' and nx['apply_days_left'] is not None:
            s.append(f'申込受付中で、締切まであと{nx["apply_days_left"]}日です。')
        elif nx['apply_status'] == 'before' and nx.get('apply_start'):
            a = _parse(nx['apply_start'])
            s.append(f'申込は{a.month}月{a.day}日から始まります。')
    elif q.get('schedule_text'):
        s.append(f'{name}の試験は{q["schedule_text"]}です。')
    line = _latest_line(q)
    if line:
        s.append(line)
    st = pass_rate_stats(q)
    if st:
        if 'diff_prev' in st and abs(st['diff_prev']) >= 3:
            word = '上昇' if st['diff_prev'] > 0 else '低下'
            s.append(f'前回から{abs(st["diff_prev"])}ポイント{word}しました。')
    if q.get('fee_yen'):
        s.append(f'受験料は{q["fee_yen"]:,}円です。')
    return s


def faqs(q: dict, today: date) -> list[dict]:
    """データから答えられる質問だけを FAQ にする (答えがない質問は出さない)。"""
    out = []
    name = q['name']
    ups = upcoming_exams(q, today)
    if ups:
        nx = ups[0]
        a = f'次回は{nx["date"].year}年{nx["date"].month}月{nx["date"].day}日です。'
        if nx.get('apply_start') and nx.get('apply_end'):
            s, e = _parse(nx['apply_start']), _parse(nx['apply_end'])
            a += f'申込期間は{s.month}月{s.day}日〜{e.month}月{e.day}日です。'
        out.append({'q': f'{name}の次の試験日はいつですか？', 'a': a})
    if q.get('schedule_text'):
        out.append({'q': f'{name}は年に何回受けられますか？', 'a': f'{q["schedule_text"]}です。'})
    st = pass_rate_stats(q)
    if st:
        a = f'直近（{st["latest"]["label"]}）は{st["latest"]["rate"]}%です。'
        if st['count'] >= 3:
            a += (f'過去{st["count"]}回では最低{st["min"]["rate"]}%（{st["min"]["label"]}）、'
                  f'最高{st["max"]["rate"]}%（{st["max"]["label"]}）でした。')
        out.append({'q': f'{name}の合格率はどれくらいですか？', 'a': a})
    elif has_pass_rates(q):
        out.append({'q': f'{name}の合格率はどれくらいですか？', 'a': _latest_line(q)})
    if q.get('fee_yen'):
        a = f'{q["fee_yen"]:,}円です。'
        if q.get('fee_note'):
            a += q['fee_note']
        out.append({'q': f'{name}の受験料はいくらですか？', 'a': a})
    if q.get('eligibility'):
        out.append({'q': f'{name}に受験資格はありますか？', 'a': q['eligibility']})
    return out


def is_indexable(q: dict, today: date) -> tuple[bool, list[str]]:
    """検索エンジンに出してよいページか。出さない理由も返す。

    データが揃っていないページを大量に公開すると、サイト全体が
    「中身の薄い量産ページ」と判定されるリスクがあるため、厳しめに判定する。
    """
    reasons = []
    if not q.get('sources'):
        reasons.append('出典がない')
    if not q.get('fee_yen'):
        reasons.append('受験料が未確認')
    if not has_pass_rates(q):
        reasons.append('合格率が未確認')
    if not upcoming_exams(q, today) and not q.get('schedule_text'):
        reasons.append('試験日程が未確認')
    checked = _parse(q.get('checked_at'))
    if not checked:
        reasons.append('確認日がない')
    elif today - checked > timedelta(days=STALE_AFTER_DAYS):
        reasons.append(f'確認日が{STALE_AFTER_DAYS}日以上前')
    return (not reasons, reasons)
