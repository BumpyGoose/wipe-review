"""Live watcher: polls a report that's being live-logged and reviews each
boss pull once it has finished. Runs on a background thread and reports back
through callbacks, so the UI never blocks on the network."""

import threading
import time
from datetime import datetime
from pathlib import Path

from . import analysis
from .wcl import WclClient

REVIEW_DIR = Path(__file__).resolve().parent.parent / "reviews"


def save_review(code, lines):
    REVIEW_DIR.mkdir(exist_ok=True)
    with open(REVIEW_DIR / f"{code}.txt", "a", encoding="utf-8") as fh:
        fh.write("\n".join(l.text for l in lines) + "\n")


class ReportRun(threading.Thread):
    """Reviews every boss pull of a report (finished or still being logged),
    then builds the evening summary. on_pull(PullResult) gets each pull in
    order, on_summary(list[Line]) the summary, on_status(str) progress, and
    on_stopped(error | None) fires once at the end."""

    def __init__(self, code, on_pull, on_summary, on_status, on_stopped, *, include_kills=True, detail_deaths=8):
        super().__init__(daemon=True)
        self.code = code
        self.on_pull, self.on_summary, self.on_status, self.on_stopped = on_pull, on_summary, on_status, on_stopped
        self.include_kills = include_kills
        self.detail_deaths = detail_deaths
        self._stop_event = threading.Event()
        self.client = WclClient()

    def stop(self):
        self._stop_event.set()

    def run(self):
        error = None
        try:
            self.on_status("Loading report...")
            report, results = analysis.analyze_report(
                self.client, self.code, self.include_kills, self.detail_deaths,
                on_progress=lambda done, total, f: self.on_status(f"Reviewed {done}/{total} pulls - {f['name']}"),
                on_result=self.on_pull, should_stop=self._stop_event.is_set)
            if not self._stop_event.is_set():
                summary = analysis.summarize_report(report, results, self.include_kills, self.detail_deaths)
                save_review(self.code, summary + [l for r in results for l in r.lines])
                self.on_summary(summary)
        except Exception as e:
            error = str(e)
        self.on_stopped(error)


class LiveWatcher(threading.Thread):
    """on_lines(list[Line]) gets each review; on_status(str) gets a one-line
    status after every poll; on_stopped(str | None) fires once at the end
    with an error message, or None if it was stopped by the user."""

    def __init__(self, code, on_lines, on_status, on_stopped, *, include_kills=False,
                 review_existing=False, detail_deaths=8, poll_seconds=10, settle_seconds=20):
        super().__init__(daemon=True)
        self.code = code
        self.on_lines, self.on_status, self.on_stopped = on_lines, on_status, on_stopped
        self.include_kills = include_kills
        self.review_existing = review_existing
        self.detail_deaths = detail_deaths
        self.poll_seconds = poll_seconds
        self.settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self.client = WclClient()

    def stop(self):
        self._stop_event.set()

    def run(self):
        seen = set()       # fight IDs already reviewed (or skipped)
        last_end = {}      # fight ID -> (endTime, wall-clock time we first saw that endTime)
        first_poll = True
        failures = 0
        error = None
        while not self._stop_event.is_set():
            try:
                report = analysis.get_report_fights(self.client, self.code)
                failures = 0
                fights = report.get("fights") or []
                if first_poll:
                    first_poll = False
                    if not self.review_existing:
                        # Pulls that were already over when we started aren't reviewed.
                        done = [f["id"] for f in fights if (report["endTime"] - f["endTime"]) / 1000 >= self.settle_seconds]
                        seen.update(done)
                        if done:
                            self.on_lines([analysis.Line(f"Skipped {len(done)} pull(s) already in the log - tick "
                                                         "'Review pulls already in the log' to include them.", "dim")])

                for f in fights:
                    if self._stop_event.is_set():
                        break
                    if f["id"] in seen:
                        continue
                    if not analysis.is_reviewable(f, self.include_kills):
                        if f.get("kill") is not None:
                            seen.add(f["id"])
                        continue
                    # Finished = the log has carried on well past the pull's end,
                    # or its end hasn't moved for a while.
                    prev = last_end.get(f["id"])
                    if not prev or prev[0] != f["endTime"]:
                        prev = last_end[f["id"]] = (f["endTime"], time.time())
                    past_end = (report["endTime"] - f["endTime"]) / 1000
                    if past_end >= self.settle_seconds or time.time() - prev[1] >= self.settle_seconds:
                        seen.add(f["id"])
                        self.on_status(f"Reviewing {f['name']} (fight {f['id']})...")
                        try:
                            lines = analysis.review_pull(self.client, self.code, f["id"], self.detail_deaths)
                            save_review(self.code, lines)
                            self.on_lines(lines)
                        except Exception as e:  # one bad pull shouldn't stop the watcher
                            self.on_lines([analysis.Line(f"Review of fight {f['id']} failed: {e}", "bad")])

                rl = report["rateLimit"]
                boss_pulls = sum(1 for f in fights if f.get("encounterID"))
                self.on_status(f"Watching \"{report.get('title') or self.code}\" - {boss_pulls} boss pulls - "
                               f"checked {datetime.now():%H:%M:%S} - API {rl['pointsSpentThisHour']:.0f}/{rl['limitPerHour']} pts this hour")
            except Exception as e:
                failures += 1
                if failures >= 5 or isinstance(e, ValueError) or first_poll:
                    error = str(e)
                    break
                self.on_status(f"Poll failed ({e}) - retrying...")
            self._stop_event.wait(self.poll_seconds)
        self.on_stopped(error)
