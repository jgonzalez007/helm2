"""HELM 2.0 entry point.

    python run.py --demo     fixtures only, no accounts, no network (default)
    python run.py --live     real sources (phase 2 onward)
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    p = argparse.ArgumentParser(prog="helm", description="HELM 2.0")
    p.add_argument("--demo", action="store_true", help="run on fixtures (default)")
    p.add_argument("--live", action="store_true", help="use real sources (phase 2 onward)")
    p.add_argument("--db", default=None, help="override the SQLite path")
    args = p.parse_args()

    if args.live:
        print("Live sources arrive in phase 2. Running demo mode instead.", file=sys.stderr)

    from helm.app import run
    run(demo=True, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
