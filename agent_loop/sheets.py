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
SHEET_POLICY = '②新テイスト方針指示書'
SHEET_QUEUE = '③投稿待ち記事'
SHEET_PUBLISHED = '④公開済み記事'
SHEET_FEEDBACK = '⑤フィードバック'

QUEUE_HEADER = [
    'id', 'created_at', 'title', 'body_md',
    'status', 'hatena_url', 'posted_at', 'error',
]
CURRENT_HEADER = [
    'ts', 'date', 'page_path', 'page_title',
    'views', 'avg_engagement_sec', 'gemini_summary',
]
PUBLISHED_HEADER = ['posted_at', 'title', 'url']
FEEDBACK_HEADER = ['date', 'article_url_or_topic', 'feedback']


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
    for name in (SHEET_CURRENT, SHEET_POLICY, SHEET_QUEUE, SHEET_PUBLISHED, SHEET_FEEDBACK):
        if name not in existing:
            requests_body.append({'addSheet': {'properties': {'title': name}}})
    if requests_body:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={'requests': requests_body}
        ).execute()

    for name, header in (
        (SHEET_CURRENT, CURRENT_HEADER),
        (SHEET_QUEUE, QUEUE_HEADER),
        (SHEET_PUBLISHED, PUBLISHED_HEADER),
        (SHEET_FEEDBACK, FEEDBACK_HEADER),
    ):
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


def read_current_recent(limit: int = 200) -> list[list[Any]]:
    """①現状データから直近 limit 行を返す (ヘッダ除く)。"""
    svc = _service()
    rng = f"'{SHEET_CURRENT}'!A1:G"
    got = svc.spreadsheets().values().get(spreadsheetId=_spreadsheet_id(), range=rng).execute()
    values = got.get('values', [])
    if len(values) <= 1:
        return []
    return values[1:][-limit:]


def read_policy() -> str:
    """②新テイスト方針指示書 A1 セルの内容を返す。空なら ''。"""
    svc = _service()
    got = svc.spreadsheets().values().get(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_POLICY}'!A1",
    ).execute()
    values = got.get('values', [])
    if not values or not values[0]:
        return ''
    return values[0][0]


def write_policy(text: str) -> None:
    """②新テイスト方針指示書 A1 セルに上書き。"""
    svc = _service()
    svc.spreadsheets().values().update(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_POLICY}'!A1",
        valueInputOption='RAW',
        body={'values': [[text]]},
    ).execute()


def list_queue_titles() -> list[str]:
    """③投稿待ち記事の全タイトル (重複検出用)。"""
    svc = _service()
    got = svc.spreadsheets().values().get(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_QUEUE}'!C2:C",
    ).execute()
    return [row[0] for row in got.get('values', []) if row]


def append_queue_article(article_id: str, created_at: str, title: str, body_md: str) -> None:
    """③投稿待ち記事の末尾に1行追加。status は空 = pending 扱い。"""
    svc = _service()
    svc.spreadsheets().values().append(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_QUEUE}'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [[article_id, created_at, title, body_md, '', '', '', '']]},
    ).execute()


def backfill_published_from_queue() -> int:
    """③で status=posted な記事を ④ に取り込む (URL重複は無視)。

    一度だけ走らせれば既存ブログ記事を内部リンクの素材に使えるようになる。
    冪等: 既に ④ に存在する URL は再追加しない。
    """
    svc = _service()
    sid = _spreadsheet_id()
    # ③ の posted 行
    q = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{SHEET_QUEUE}'!A1:H",
    ).execute().get('values', [])
    # ④ に既にあるURL
    p = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{SHEET_PUBLISHED}'!A2:C",
    ).execute().get('values', [])
    have = {r[2] for r in p if len(r) >= 3 and r[2]}

    added: list[list[str]] = []
    for row in q[1:]:
        padded = row + [''] * (8 - len(row))
        _id, _created, title, _body, status, hatena_url, posted_at, _err = padded[:8]
        if status != 'posted' or not hatena_url or not title:
            continue
        if hatena_url in have:
            continue
        added.append([posted_at or '', title, hatena_url])
        have.add(hatena_url)

    if added:
        svc.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{SHEET_PUBLISHED}'!A1",
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': added},
        ).execute()
    return len(added)


def record_published(title: str, url: str, posted_at: str) -> None:
    """④公開済み記事の末尾に1行追加。"""
    svc = _service()
    svc.spreadsheets().values().append(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_PUBLISHED}'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [[posted_at, title, url]]},
    ).execute()


def list_published(limit: int = 50) -> list[dict[str, str]]:
    """④公開済み記事を新しい順で limit 件返す。"""
    svc = _service()
    got = svc.spreadsheets().values().get(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_PUBLISHED}'!A2:C",
    ).execute()
    rows = got.get('values', [])
    out: list[dict[str, str]] = []
    for r in rows:
        padded = r + [''] * (3 - len(r))
        posted_at, title, url = padded[:3]
        if title and url:
            out.append({'posted_at': posted_at, 'title': title, 'url': url})
    return out[::-1][:limit]


def read_feedback(limit: int = 10) -> list[dict[str, str]]:
    """⑤フィードバックを新しい順で limit 件返す。"""
    svc = _service()
    got = svc.spreadsheets().values().get(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_FEEDBACK}'!A2:C",
    ).execute()
    rows = got.get('values', [])
    out: list[dict[str, str]] = []
    for r in rows:
        padded = r + [''] * (3 - len(r))
        d, target, fb = padded[:3]
        if fb.strip():
            out.append({'date': d, 'target': target, 'feedback': fb})
    return out[::-1][:limit]


def mark_failed(row: int, error: str) -> None:
    svc = _service()
    svc.spreadsheets().values().update(
        spreadsheetId=_spreadsheet_id(),
        range=f"'{SHEET_QUEUE}'!E{row}:H{row}",
        valueInputOption='RAW',
        body={'values': [['failed', '', '', error[:500]]]},
    ).execute()
