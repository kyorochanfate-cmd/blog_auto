"""Entrypoint for the AI agent loop.

Modes (in execution order for --all):
  --fetch-data  A: GA4 → ①現状データ
  --analyze     B: ①現状データ → Gemini → ②新テイスト方針指示書
  --write       C: ②方針 → Gemini → ③投稿待ち記事
  --publish     D: ③投稿待ち記事 → Hatena AtomPub
  --all         A → B → C → D を順次実行
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / '.env')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Hatena auto-agent loop')
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--fetch-data', action='store_true', help='A: GA4 を集計してシートに追記')
    g.add_argument('--analyze', action='store_true', help='B: 傾向分析 → 方針更新')
    g.add_argument('--write', action='store_true', help='C: 方針 → 記事執筆 → 投稿待ち追記')
    g.add_argument('--publish', action='store_true', help='D: 投稿待ち記事を公開')
    g.add_argument('--all', action='store_true', help='A → B → C → D を順次実行')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    from . import analytics_hub, article_writer, hatena_publisher, trend_analyzer

    rc = 0

    if args.fetch_data or args.all:
        try:
            n = analytics_hub.run()
            print(f'[fetch-data] appended_rows={n}')
        except Exception as e:
            logging.exception('fetch-data failed')
            print(f'[fetch-data] ERROR: {e}', file=sys.stderr)
            rc = 1

    if args.analyze or args.all:
        try:
            text = trend_analyzer.run()
            print(f'[analyze] policy_chars={len(text)}')
        except Exception as e:
            logging.exception('analyze failed')
            print(f'[analyze] ERROR: {e}', file=sys.stderr)
            rc = 1

    if args.write or args.all:
        try:
            title = article_writer.run()
            print(f'[write] title={title!r}')
        except Exception as e:
            logging.exception('write failed')
            print(f'[write] ERROR: {e}', file=sys.stderr)
            rc = 1

    if args.publish or args.all:
        try:
            ok, ng = hatena_publisher.run()
            print(f'[publish] success={ok} failed={ng}')
            if ng:
                rc = max(rc, 2)
        except Exception as e:
            logging.exception('publish failed')
            print(f'[publish] ERROR: {e}', file=sys.stderr)
            rc = 1

    return rc


if __name__ == '__main__':
    sys.exit(main())
