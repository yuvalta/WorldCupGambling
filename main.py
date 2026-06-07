"""Orchestration + CLI for the World Cup daily score-prediction emailer.

Flows:
  python main.py --dry-run            today's picks, printed (no email)
  python main.py --date 2026-06-15    a specific day
  python main.py                      emailed (needs SMTP env vars)
  python main.py --score 2026-06-14   accuracy of that day's picks vs results
  python main.py --date <d> --debug   verbose Gamma matching for each fixture
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import List, Optional

import config
from notify import DigestItem, render_digest, send_email
from polymarket import PolymarketClient
from predict import Totals, build_prediction
from schedule_source import Match, OpenFootballSource, ScheduleSource
from store import append_predictions, record_from, score_day


def _today() -> str:
    return dt.date.today().isoformat()


def build_items(
    date: str,
    schedule: ScheduleSource,
    market_client: PolymarketClient,
    debug: bool = False,
) -> List[DigestItem]:
    """Fetch fixtures for `date`, attach market snapshot + prediction."""
    matches = schedule.matches_on(date)
    items: List[DigestItem] = []
    for m in matches:
        snap = market_client.find_market(m.team1, m.team2, debug=debug)
        if snap is None:
            items.append(DigestItem(match=m, prediction=None, note="no live market found"))
            continue
        totals = None
        if snap.line is not None and snap.p_over is not None:
            totals = Totals(line=snap.line, p_over=snap.p_over, p_under=snap.p_under or (1 - snap.p_over))
        prediction = build_prediction(
            m.team1, m.team2, snap.p1, snap.draw, snap.p2, totals=totals
        )
        items.append(DigestItem(match=m, prediction=prediction))
    return items


def run_digest(date: str, dry_run: bool, debug: bool) -> int:
    schedule = OpenFootballSource()
    client = PolymarketClient()
    items = build_items(date, schedule, client, debug=debug)

    subject, text_body, html_body = render_digest(date, items)

    # Persist the predictions we actually produced (skip the no-market ones).
    records = [
        record_from(date, it.match, it.prediction)
        for it in items
        if it.prediction is not None
    ]
    append_predictions(records)

    if dry_run:
        print(subject)
        print()
        print(text_body)
        return 0

    cfg = config.load_email_config()
    if not cfg.configured:
        print("ERROR: email not configured (set SMTP_* and EMAIL_TO). Use --dry-run to preview.", file=sys.stderr)
        return 2
    send_email(cfg, subject, text_body, html_body)
    print(f"Sent digest for {date} to {cfg.recipient} ({len(items)} match(es)).")
    return 0


def run_score(date: str) -> int:
    schedule = OpenFootballSource()
    finished = schedule.matches_on(date)
    report = score_day(date, finished)
    if report.total == 0:
        print(f"No scored picks for {date} (no stored predictions or results not in yet).")
        return 0
    print(f"Scoring {date}: {report.total} pick(s)")
    for p in report.picks:
        mark = "EXACT" if p.exact else ("outcome" if p.outcome_correct else "miss")
        print(
            f"  {p.label}: predicted {p.predicted[0]}–{p.predicted[1]}, "
            f"actual {p.actual[0]}–{p.actual[1]}  [{mark}]"
        )
    print(
        f"  -> exact scorelines: {report.exact_count}/{report.total}; "
        f"correct outcomes: {report.outcome_count}/{report.total}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="World Cup daily score-prediction emailer")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, don't email")
    parser.add_argument("--debug", action="store_true", help="verbose Gamma market matching")
    parser.add_argument("--score", metavar="YYYY-MM-DD", help="score a past day's picks vs results")
    args = parser.parse_args(argv)

    if args.score:
        return run_score(args.score)

    date = args.date or _today()
    return run_digest(date, dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
