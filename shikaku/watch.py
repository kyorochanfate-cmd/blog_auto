"""公式ページの変更を検知し、データの更新案を作る。

流れ:
  1. 各資格の official_url を取得し、本文のハッシュを前回と比べる
  2. 変わっていたら、Gemini に「現在のデータ」と「新しい本文」を渡して更新案を出させる
  3. 更新案には公式ページからの引用 (evidence) を必須にし、
     その引用が本文に実在するかをコードで確認する (AIの捏造を通さない)
  4. 通った更新案だけを承認待ちキューに出す。反映は人間の承認後 (apply 側)

使い方:
  python -m shikaku.watch --data shikaku/data/quals.json --state watch_state.json \
         --out proposals.json [--only slug1,slug2] [--no-llm]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

UA = 'Mozilla/5.0 (compatible; shikaku-calendar-bot/1.0; +https://shikaku-calendar.com/about/)'
TIMEOUT = 25
MAX_TEXT = 30_000

# 更新案として受け付ける項目 (これ以外を AI が出してきても捨てる)
ALLOWED_FIELDS = {'exams', 'fee_yen', 'fee_note', 'pass_rates', 'schedule_text', 'eligibility', 'notices'}


def fetch_text(url: str) -> tuple[str, str]:
    """(本文テキスト, 種別) を返す。PDF は本文を取らずバイト列のハッシュ用に16進を返す。"""
    for attempt in range(3):
        try:
            r = requests.get(url, headers={'User-Agent': UA}, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    ctype = r.headers.get('content-type', '')
    if 'pdf' in ctype or url.lower().endswith('.pdf'):
        return hashlib.sha256(r.content).hexdigest(), 'pdf'
    r.encoding = r.apparent_encoding or r.encoding
    html = r.text
    text = ''
    try:
        import trafilatura
        text = trafilatura.extract(html, include_tables=True, include_comments=False) or ''
    except Exception:
        text = ''
    if len(text) < 200:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        for t in soup(['script', 'style', 'noscript']):
            t.decompose()
        text = soup.get_text('\n')
    return normalize(text)[:MAX_TEXT], 'html'


def normalize(text: str) -> str:
    """全角英数・空白ゆれを吸収 (ハッシュと引用照合の両方で使う)。"""
    t = unicodedata.normalize('NFKC', text)
    t = re.sub(r'[ \t　]+', ' ', t)
    t = re.sub(r'\n\s*\n+', '\n', t)
    return t.strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


_PROMPT = """あなたは資格試験の公式情報を確認する担当者です。
以下の「現在のデータ」と、実施団体の公式ページの「最新の本文」を比べ、
データに反映すべき変更だけを提案してください。

# 厳守ルール
- 公式ページの本文に明記されていることだけを根拠にする。推測・一般知識は禁止
- 各提案には、根拠となる本文の一節を evidence に**一字一句そのまま**引用する (20〜200字)
- 変更がなければ proposals は空配列
- 対象項目: exams (試験日・申込期間), fee_yen, fee_note, pass_rates, schedule_text, eligibility, notices
- value の形式は次の通り (キー名を変えない):
  - exams: [{"label": "回の名前", "exam_date": "YYYY-MM-DD", "apply_start": "YYYY-MM-DD", "apply_end": "YYYY-MM-DD"}]
    (和暦は西暦に直す。申込期間が本文に無ければ apply_start/apply_end は省略)
  - pass_rates: [{"label": "回の名前", "rate": 数値, "examinees": 整数(任意)}] (追加すべき回だけ)
  - fee_yen: 整数 / fee_note, schedule_text, eligibility: 文字列 / notices: [文字列]
- evidence は本文の連続した一節をそのまま写す。離れた行をつなげない。複数の行が必要なら改行で区切る
- 今日は {today}。今日より前の試験日は提案しない

# 資格名
{name}

# 現在のデータ
{current}

# 公式ページの最新の本文
{text}

# 出力 (JSONのみ。コードフェンス不要)
{"proposals": [{"field": "exams|fee_yen|...", "action": "add|update", "value": ..., "evidence": "本文からの一字一句の引用", "summary": "何が変わったか1行"}]}
"""


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith('```'):
        s = s.split('\n', 1)[1] if '\n' in s else ''
        s = s.rsplit('```', 1)[0]
    return s.strip()


def propose_updates(q: dict, text: str, today: str) -> list[dict]:
    from google import genai
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    model = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    current = {k: q.get(k) for k in ('exams', 'fee_yen', 'fee_note', 'pass_rates',
                                     'schedule_text', 'eligibility', 'notices') if q.get(k) is not None}
    # str.format は本文中の { } で壊れるので replace を使う (リポジトリ共通の約束)。
    # 本文は最後に差し込む (本文中の文字列が他の差し込み位置と誤解されないように)
    prompt = (_PROMPT.replace('{today}', today)
              .replace('{name}', q['name'])
              .replace('{current}', json.dumps(current, ensure_ascii=False))
              .replace('{text}', text))
    resp = client.models.generate_content(model=model, contents=prompt)
    data = json.loads(_strip_fences(resp.text or '{}'))
    return data.get('proposals') or []


_KEY_ALIASES = {
    'date': 'exam_date', 'exam': 'exam_date', 'test_date': 'exam_date',
    'start_date': 'apply_start', 'application_start': 'apply_start', 'apply_from': 'apply_start',
    'end_date': 'apply_end', 'application_end': 'apply_end', 'deadline': 'apply_end', 'apply_to': 'apply_end',
    'name': 'label', 'pass_rate': 'rate',
}


def normalize_value(field: str, value):
    """AI がキー名を言い換えてきた場合に、サイトのデータ形式へ揃える。"""
    if field in ('exams', 'pass_rates'):
        items = value if isinstance(value, list) else [value]
        out = []
        for it in items:
            if isinstance(it, dict):
                out.append({_KEY_ALIASES.get(k, k): v for k, v in it.items()})
        return out
    return value


_ISO_DATE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


def _facts_in_text(value, text: str) -> list[str]:
    """value に含まれる日付・数値が本文に実在するか。見つからないものを返す。

    日付 2026-10-18 は「10月18日」または「10/18」として、
    受験料 7700 は「7,700」または「7700」として本文にあることを確認する。
    """
    missing = []
    blob = json.dumps(value, ensure_ascii=False)
    for y, m, d in _ISO_DATE.findall(blob):
        m_, d_ = int(m), int(d)
        if f'{m_}月{d_}日' not in text and f'{m_}/{d_}' not in text:
            missing.append(f'{y}-{m}-{d}')
    nums = []
    def walk(v, key=None):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and key != 'exam_date':
            nums.append(v)
    walk(value)
    for n in nums:
        cands = {str(n)}
        if isinstance(n, int) or float(n).is_integer():
            cands |= {f'{int(n):,}', str(int(n))}
        if not any(c in text for c in cands):
            missing.append(str(n))
    return missing


def verify(proposals: list[dict], text: str) -> tuple[list[dict], list[dict]]:
    """本文に裏付けのある提案だけ通す。

    - 項目が許可リストにあること
    - 引用 (evidence) の各行が本文にそのまま存在すること
    - 提案値の日付・数値がすべて本文に存在すること (AI の書き間違い・捏造を止める)
    """
    norm_text = normalize(text)
    ok, rejected = [], []
    for p in proposals:
        field = p.get('field')
        if field not in ALLOWED_FIELDS:
            rejected.append({**p, 'reject_reason': '対象外の項目'})
            continue
        p = {**p, 'value': normalize_value(field, p.get('value'))}
        lines = [normalize(x) for x in str(p.get('evidence') or '').splitlines()]
        lines = [x for x in lines if x]
        if sum(len(x) for x in lines) < 10:
            rejected.append({**p, 'reject_reason': '引用が短すぎる'})
            continue
        bad_line = next((x for x in lines if x not in norm_text), None)
        if bad_line is not None:
            rejected.append({**p, 'reject_reason': f'引用が公式ページに存在しない: {bad_line[:40]}'})
            continue
        missing = _facts_in_text(p.get('value'), norm_text)
        if missing:
            rejected.append({**p, 'reject_reason': f'提案値が本文に見当たらない: {", ".join(missing[:5])}'})
            continue
        ok.append(p)
    return ok, rejected


def run(data_path: Path, state_path: Path, out_path: Path,
        only: set[str] | None = None, use_llm: bool = True) -> dict:
    quals = json.loads(data_path.read_text(encoding='utf-8'))
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    report = {'checked': 0, 'unchanged': 0, 'changed': [], 'first_seen': [], 'errors': {},
              'proposals': [], 'rejected': []}

    for q in quals:
        if only and q['slug'] not in only:
            continue
        # 日程が別ページにあることも多いので、出典にある公式ページはすべて見る
        urls = list(dict.fromkeys([u for u in [q.get('official_url'), *(q.get('sources') or [])] if u]))
        if not urls:
            continue
        report['checked'] += 1
        for url in urls:
            try:
                text, kind = fetch_text(url)
            except Exception as e:
                report['errors'][f"{q['slug']} {url}"] = f'{type(e).__name__}: {e}'[:200]
                continue
            h = digest(text)
            prev = state.get(url)
            state[url] = {'hash': h, 'kind': kind, 'checked_at': now.isoformat(timespec='seconds')}
            if prev is None:
                # 初回は比較対象がないので記録だけ (初期データは人間が確認済みの前提)
                report['first_seen'].append(url)
                continue
            if prev.get('hash') == h:
                report['unchanged'] += 1
                continue
            report['changed'].append(url)
            if kind == 'pdf' or not use_llm:
                report['proposals'].append({'slug': q['slug'], 'url': url, 'field': None,
                                            'summary': '公式ページが変更されました (内容の確認が必要)'})
                continue
            try:
                props = propose_updates(q, text, today)
            except Exception as e:
                report['errors'][f"{q['slug']} {url}"] = f'LLM: {type(e).__name__}: {e}'[:200]
                continue
            ok, bad = verify(props, text)
            for p in ok:
                report['proposals'].append({'slug': q['slug'], 'url': url, **p})
            for p in bad:
                report['rejected'].append({'slug': q['slug'], 'url': url, **p})

    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(Path(__file__).parent / 'data' / 'quals.json'))
    ap.add_argument('--state', default='watch_state.json')
    ap.add_argument('--out', default='proposals.json')
    ap.add_argument('--only', default='')
    ap.add_argument('--no-llm', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    only = {s for s in a.only.split(',') if s} or None
    r = run(Path(a.data), Path(a.state), Path(a.out), only, not a.no_llm)
    print(f'checked={r["checked"]} unchanged={r["unchanged"]} first_seen={len(r["first_seen"])} '
          f'changed={len(r["changed"])} proposals={len(r["proposals"])} '
          f'rejected={len(r["rejected"])} errors={len(r["errors"])}')
    for slug, e in r['errors'].items():
        print(f'  error {slug}: {e}')


if __name__ == '__main__':
    main()
