"""承認待ちシート (Google スプレッドシート) との読み書き。

スマホからでも承認できるよう、承認の操作はスプレッドシートの
「判定」列を「承認」に変えるだけにしている。

列: A=ID  B=検出日  C=資格  D=項目  E=変更内容  F=根拠(公式ページの引用)
    G=公式URL  H=判定(承認/却下)  I=反映日  J=提案データ(JSON, 触らない)

環境変数:
  SHIKAKU_REVIEW_SHEET_ID         承認待ちスプレッドシートのID
  GOOGLE_APPLICATION_CREDENTIALS  サービスアカウントJSON (シートを編集者で共有しておく)
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date

SHEET = '承認待ち'
HEADER = ['ID', '検出日', '資格', '項目', '変更内容', '根拠（公式ページの引用）',
          '公式URL', '判定（承認/却下）', '反映日', '提案データ（編集しない）']
FIELD_JA = {
    'exams': '試験日程', 'fee_yen': '受験料', 'fee_note': '受験料の補足',
    'pass_rates': '合格率', 'schedule_text': '試験の頻度', 'eligibility': '受験資格',
    'notices': 'お知らせ', None: '要確認',
}


def _svc():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'],
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def proposal_id(p: dict) -> str:
    """同じ提案を毎日重複して追加しないための ID。"""
    key = json.dumps([p.get('slug'), p.get('field'), p.get('value'), p.get('summary')],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]


def _ensure_sheet(svc, sid: str) -> None:
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    titles = {s['properties']['title'] for s in meta.get('sheets', [])}
    if SHEET not in titles:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={
            'requests': [{'addSheet': {'properties': {'title': SHEET}}}]}).execute()
    got = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{SHEET}'!A1:J1").execute()
    if not got.get('values'):
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{SHEET}'!A1", valueInputOption='RAW',
            body={'values': [HEADER]}).execute()


def _rows(svc, sid: str) -> list[list[str]]:
    got = svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{SHEET}'!A2:J").execute()
    return got.get('values', [])


def push(sid: str, proposals: list[dict], names: dict[str, str]) -> int:
    """新しい提案だけを末尾に追加。追加件数を返す。"""
    svc = _svc()
    _ensure_sheet(svc, sid)
    existing = {r[0] for r in _rows(svc, sid) if r}
    new_rows = []
    for p in proposals:
        pid = proposal_id(p)
        if pid in existing:
            continue
        existing.add(pid)
        value = p.get('value')
        change = p.get('summary') or ''
        if value is not None:
            change += f'\n→ {json.dumps(value, ensure_ascii=False)}'
        new_rows.append([
            pid, date.today().isoformat(), names.get(p['slug'], p['slug']),
            FIELD_JA.get(p.get('field'), p.get('field') or ''), change.strip(),
            p.get('evidence', ''), p.get('url', ''), '', '',
            json.dumps(p, ensure_ascii=False),
        ])
    if new_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"'{SHEET}'!A1", valueInputOption='RAW',
            insertDataOption='INSERT_ROWS', body={'values': new_rows}).execute()
    return len(new_rows)


def pull_approved(sid: str) -> list[tuple[int, dict]]:
    """判定が「承認」で、まだ反映していない提案を (行番号, 提案) で返す。"""
    svc = _svc()
    _ensure_sheet(svc, sid)
    out = []
    for i, r in enumerate(_rows(svc, sid), start=2):
        r = r + [''] * (10 - len(r))
        if r[7].strip() == '承認' and not r[8].strip() and r[9]:
            try:
                p = json.loads(r[9])
            except json.JSONDecodeError:
                continue
            if p.get('field'):
                out.append((i, {**p, 'approved': True}))
    return out


def mark_applied(sid: str, row_numbers: list[int]) -> None:
    if not row_numbers:
        return
    svc = _svc()
    today = date.today().isoformat()
    data = [{'range': f"'{SHEET}'!I{n}", 'values': [[today]]} for n in row_numbers]
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={'valueInputOption': 'RAW', 'data': data}).execute()
