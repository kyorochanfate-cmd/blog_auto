"""Entrypoint for the AI agent loop.

Usage:
  python -m agent_loop.main --fetch-data    # GA4 → Sheets
  python -m agent_loop.main --publish       # Sheets → Hatena
  python -m agent_loop.main --all           # 両方順番に
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
    g.add_argument('--fetch-data', action='store_true', help='GA4 を集計してシートに追記')
    g.add_argument('--publish', action='store_true', help='シートの投稿待ち記事を公開')
    g.add_argument('--all', action='store_true', help='fetch → publish の順で両方')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    from . import analytics_hub, hatena_publisher

    rc = 0
    if args.fetch_data or args.all:
        try:
            n = analytics_hub.run()
            print(f'[fetch-data] appended_rows={n}')
        except Exception as e:
            logging.exception('fetch-data failed')
            print(f'[fetch-data] ERROR: {e}', file=sys.stderr)
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
