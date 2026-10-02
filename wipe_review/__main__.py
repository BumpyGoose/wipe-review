"""Command-line helpers (the live UI is app.py):

    python -m wipe_review review   <report url or code> <fight ids>   # e.g. 46 or 44,46,47
    python -m wipe_review replay   <report url or code> [--kills]     # every pull in the report
    python -m wipe_review report   <report url or code> [--wipes-only] # evening summary, then every pull
    python -m wipe_review discover <report url or code> <fight ids>   # list ability/debuff/cast IDs for data/bosses.json
"""

import argparse
import sys

from . import analysis
from .wcl import WclClient

ANSI = {"normal": "", "dim": "\033[90m", "info": "\033[36m", "header": "\033[96;1m", "kill": "\033[92;1m",
        "warn": "\033[93m", "bad": "\033[91m"}


def show(lines, color):
    for l in lines:
        print(f"{ANSI[l.tag]}{l.text}\033[0m" if color and ANSI[l.tag] else l.text)


def main():
    p = argparse.ArgumentParser(prog="python -m wipe_review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["review", "replay", "report", "discover"])
    p.add_argument("report", help="report URL or code")
    p.add_argument("fights", nargs="?", default="", help="comma-separated fight IDs")
    p.add_argument("--kills", action="store_true", help="replay: include kills")
    p.add_argument("--wipes-only", action="store_true", help="report: skip kills")
    p.add_argument("--detail", type=int, default=8, help="deaths analysed in full per pull")
    p.add_argument("--no-color", action="store_true")
    a = p.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    color = not a.no_color and sys.stdout.isatty()
    code = analysis.report_code(a.report)
    if not code:
        p.error("that doesn't look like a report URL or code")
    fights = [int(x) for x in a.fights.split(",") if x.strip()]
    client = WclClient()

    if a.command == "discover":
        if not fights:
            p.error("discover needs fight IDs")
        show(analysis.discover(client, code, fights), color)
    elif a.command == "review":
        if not fights:
            p.error("review needs fight IDs")
        for f in fights:
            show(analysis.review_pull(client, code, f, a.detail), color)
    elif a.command == "report":
        include_kills = not a.wipes_only
        progress = lambda done, total, f: print(f"\r  reviewed {done}/{total} pulls", end="", file=sys.stderr, flush=True)
        report, results = analysis.analyze_report(client, code, include_kills, a.detail, on_progress=progress)
        print(file=sys.stderr)
        show(analysis.summarize_report(report, results, include_kills, a.detail), color)
        for r in results:
            show(r.lines, color)
    else:
        report = analysis.get_report_fights(client, code)
        for f in report["fights"]:
            if analysis.is_reviewable(f, a.kills):
                show(analysis.review_pull(client, code, f["id"], a.detail), color)


if __name__ == "__main__":
    main()
