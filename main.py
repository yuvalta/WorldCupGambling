"""Orchestration + CLI for the World Cup daily score-prediction emailer.

Flows:
  python main.py --dry-run            today's picks, printed (no send)
  python main.py --date 2026-06-15    a specific day
  python main.py                      sent (email and/or Telegram, if configured)
  python main.py --score 2026-06-14   accuracy of that day's picks vs results
  python main.py --date <d> --debug   verbose Gamma matching for each fixture

Sends are best-effort: a failed email or Telegram push is logged but does not
crash the run, so the predictions log still persists for next-day scoring.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import List, Optional

import config
import core
from notify import render_digest, send_email, send_telegram
from schedule_source import OpenFootballSource
from store import ScoreReport, append_predictions, record_from, score_day


def _today() -> str:
    return dt.date.today().isoformat()


def _push_telegram(text: str) -> None:
    """Best-effort Telegram push. Logs and swallows errors."""
    cfg = config.load_telegram_config()
    if not cfg.configured:
        return
    try:
        send_telegram(cfg, text)
        print("Telegram push sent.")
    except Exception as exc:  # noqa: BLE001 - non-fatal by design
        print(f"WARNING: Telegram push failed: {exc}", file=sys.stderr)


def _push_email(subject: str, text_body: str, html_body: str) -> bool:
    """Best-effort email send. Returns True if attempted+ok, False if skipped."""
    cfg = config.load_email_config()
    if not cfg.configured:
        return False
    try:
        send_email(cfg, subject, text_body, html_body)
        print(f"Email sent to {cfg.recipient}.")
        return True
    except Exception as exc:  # noqa: BLE001 - non-fatal by design
        print(f"WARNING: email send failed: {exc}", file=sys.stderr)
        return False


def run_digest(date: str, dry_run: bool, debug: bool) -> int:
    items = core.build_items(date, debug=debug)
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

    sent_email = _push_email(subject, text_body, html_body)
    _push_telegram(text_body)

    tg = config.load_telegram_config()
    if not sent_email and not tg.configured:
        print(
            "ERROR: no delivery channel configured (set SMTP_* / EMAIL_TO or "
            "TELEGRAM_*). Use --dry-run to preview.",
            file=sys.stderr,
        )
        return 2
    print(f"Digest for {date} processed ({len(items)} match(es)).")
    return 0


def _format_score_text(report: ScoreReport) -> str:
    lines = [f"World Cup results — {report.date}", "-" * 32]
    for p in report.picks:
        mark = "EXACT" if p.exact else ("outcome" if p.outcome_correct else "miss")
        lines.append(
            f"{p.label}: predicted {p.predicted[0]}–{p.predicted[1]}, "
            f"actual {p.actual[0]}–{p.actual[1]}  [{mark}]"
        )
    lines.append("-" * 32)
    lines.append(
        f"Exact scorelines: {report.exact_count}/{report.total}; "
        f"correct outcomes: {report.outcome_count}/{report.total}"
    )
    return "\n".join(lines)


def run_score(date: str, dry_run: bool = False) -> int:
    schedule = OpenFootballSource()
    finished = schedule.matches_on(date)
    report = score_day(date, finished)
    if report.total == 0:
        print(f"No scored picks for {date} (no stored predictions or results not in yet).")
        return 0

    text = _format_score_text(report)
    print(text)
    if not dry_run:
        _push_telegram(text)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="World Cup daily score-prediction emailer")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="print only, don't send")
    parser.add_argument("--debug", action="store_true", help="verbose Gamma market matching")
    parser.add_argument("--score", metavar="YYYY-MM-DD", help="score a past day's picks vs results")
    args = parser.parse_args(argv)

    if args.score:
        return run_score(args.score, dry_run=args.dry_run)

    date = args.date or _today()
    return run_digest(date, dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
