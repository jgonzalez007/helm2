"""HELM 2.0 entry point.

    python run.py --demo     fixtures only, no accounts, no network (default)
    python run.py --live     real sources (phase 2 onward)
"""

from __future__ import annotations

import argparse


def main() -> int:
    p = argparse.ArgumentParser(prog="helm", description="HELM 2.0")
    p.add_argument("--demo", action="store_true", help="run on fixtures (default)")
    p.add_argument("--live", action="store_true",
                   help="real calendar through your approved connectors (phase 2)")
    p.add_argument("--db", default=None, help="override the SQLite path")
    p.add_argument("--check", action="store_true",
                   help="report what the connectors can do, then exit")
    args = p.parse_args()

    if args.check:
        from helm import claude_cli as cli
        av = cli.probe_availability()
        print(f"claude CLI:    {av.cli or 'NOT FOUND'}")
        for which in ("Microsoft 365", "Slack"):
            reason = av.reason(which)
            print(f"{which + ':':15}{'connected' if not reason else reason}")
        if av.error:
            print(f"error:         {av.error}")
        return 0 if av.microsoft_365 else 1

    from helm.app import run
    run(demo=not args.live, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
