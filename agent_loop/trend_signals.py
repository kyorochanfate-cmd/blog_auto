"""外部トレンドソース。

自社GA4のデータが薄い「種まき期」用に、外部のリアルタイム話題ソースから
旬のトピックを取得する。`src/trends.py` を再利用:
  はてブ テクノロジー / Reddit r/gadgets / r/technology / r/apple / r/Android /
  Hacker News
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from src import trends as _src_trends  # type: ignore
except Exception as _e:
    _src_trends = None
    logging.getLogger(__name__).warning('src.trends unavailable: %s', _e)

log = logging.getLogger(__name__)


def fetch_trending(per_source: int = 6, hours: int = 72) -> list[dict]:
    """外部ソースから旬のトピックを取得。

    Returns:
      [{source, title, url, summary}, ...]
      失敗時は []
    """
    if _src_trends is None:
        return []
    try:
        items = _src_trends.fetch_trends(per_source=per_source, hours=hours)
    except Exception as e:
        log.exception('fetch_trends failed: %s', e)
        return []
    out = []
    for it in items[:per_source * 6]:
        out.append({
            'source': it.get('source', ''),
            'title': it.get('title', ''),
            'url': it.get('url', ''),
            'summary': it.get('summary', '')[:200],
        })
    return out


def format_for_prompt(items: list[dict], limit: int = 25) -> str:
    """インデックス付きで整形（Gemini が source index で参照できるよう）。"""
    if not items:
        return '(外部トレンドデータなし)'
    lines = []
    for i, it in enumerate(items[:limit]):
        lines.append(f"[{i}] [{it['source']}] {it['title']}  ({it['url']})")
    return '\n'.join(lines)


def fetch_source_bodies(items: list[dict], indices: list[int],
                        per_source_chars: int = 1800) -> list[dict]:
    """指定インデックスの記事本文を取得。

    src/researcher.fetch_article_texts() を再利用。
    Returns: [{title, url, text}, ...]
    """
    sys_path_init = str(Path(__file__).resolve().parent.parent)
    if sys_path_init not in sys.path:
        sys.path.insert(0, sys_path_init)
    try:
        from src import researcher  # type: ignore
    except Exception as e:
        log.warning('researcher unavailable: %s', e)
        return []

    selected = []
    for idx in indices:
        if 0 <= idx < len(items):
            it = items[idx]
            selected.append({
                'title': it.get('title', ''),
                'link': it.get('url', ''),
                'summary': it.get('summary', ''),
            })
    if not selected:
        return []

    try:
        sources = researcher.fetch_article_texts(
            selected,
            max_items=len(selected),
            per_source_chars=per_source_chars,
        )
    except Exception as e:
        log.exception('fetch_article_texts failed: %s', e)
        return []

    out = []
    for s in sources:
        text = s.get('text', '') or s.get('body', '')
        if text:
            out.append({
                'title': s.get('title', ''),
                'url': s.get('url', '') or s.get('link', ''),
                'text': text,
            })
    return out
