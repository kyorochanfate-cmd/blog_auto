"""Google Sheets I/O for the AI agent loop.

シート構成 (1つの SPREADSHEET_ID 内に以下の3タブ):

  ①現状データ      : ts | date | page_path | page_title | views | avg_engagement_sec | gemini_summary
  ②新テイスト方針指示書 : Claude Pro が Web UI から手動で更新するので Python は読まない
  ③投稿待ち記事    : id | created_at | title | body_md | status | hatena_url | posted_at | error
                    status: pending / posted / failed
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

SHEET_CURRENT = '①現状データ'
SHEET_QUEUE = '③投稿待ち記事'

QUEUE_HEADER = [
    'id', 'created_at', 'title', 'body_md',
    'status', 'hatena_url', 'posted_at', 'error',
]
CURRENT_HEADER = [
    'ts', 'date', 'page_path', 'page_title',
    'views', 'avg_engagement_sec', 'gemini_summary',
]


@dataclass
class QueuedArticle:
    row: int  # 1-indexed sheet row
    id: str
    title: str
    body_md: str


def _service():
    creds_path = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _spreadsheet_id() -> str:
    return os.environ['SPREADSHEET_ID']


def ensure_headers() -> None:
    """初回実行時に各シートのヘッダ行を整える。"""
    svc = _service()
    sid = _spreadsheet_id()
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    existing = {s['properties']['title'] for s in meta.get('sheets', [])}

    requests_body: list[dict[str, Any]] = []
    for name in (SHEET_CURRENT, SHEET_QUEUE):
        if name not in existing:
            requests_body.append({'addSheet': {'properties': {'title': name}}})
    if requests_body:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={'requests': requests_body}
        ).execute()

    for name, header in ((SHEET_CURRENT, CURRENT_HEADER), (SHEET_QUEUE, QUEUE_HEADER)):
        rng = f"'{name}'!1:1"
        got = svc.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute()
        if not got.get('values'):
            svc.spreadsheets().values().update(
                spreadsheetId=sid,
                range=rng,
                valueInputOption='RAW',
                body={'values': [header]},
            ).execute()


def append_current(rows: list[list[Any]]) -> None:
    if not rows:
        return
    svc = _service()
    svc.spreadsheets().values().append(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_CURRENT}'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': rows},
    ).execute()


def fetch_pending_articles() -> list[QueuedArticle]:
    """status が空 or 'pending' な行を返す。"""
    svc = _service()
    rng = f"'{SHEET_QUEUE}'!A1:H"
    got = svc.spreadsheets().values().get(spreadsheetId=_spreadsheet_id(), range=rng).execute()
    values = got.get('values', [])
    if len(values) <= 1:
        return []
    out: list[QueuedArticle] = []
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [''] * (len(QUEUE_HEADER) - len(row))
        rid, _created, title, body, status, *_ = padded
        if status and status.lower() not in ('', 'pending'):
            continue
        if not title or not body:
            continue
        out.append(QueuedArticle(row=idx, id=rid or f'row{idx}', title=title, body_md=body))
    return out


def mark_posted(row: int, hatena_url: str, posted_at: str) -> None:
    svc = _service()
    svc.spreadsheets().values().update(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_QUEUE}'!E{row}:H{row}",
        valueInputOption='RAW',
        body={'values': [['posted', hatena_url, posted_at, '']]},
    ).execute()


def mark_failed(row: int, error: str) -> None:
    svc = _service()
    svc.spreadsheets().values().update(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_QUEUE}'!E{row}:H{row}",
        valueInputOption='RAW',
        body={'values': [['failed', '', '', error[:500]]]},
    ).execute()
