"""Flask dashboard for the World Cup predictor.

Live per-request: each page load fetches today's fixtures, market odds, and
runs the Poisson model via `core.build_items`. Stateless — no cache, no writes.
The accuracy panel reads the persisted predictions log (written by the daily
job) and scores it against real results.

Honesty rule (kept in the UI): probabilities are Polymarket crowd odds; the
scoreline is our own independent-Poisson model, not a Polymarket prediction.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from flask import Flask, abort, jsonify, redirect, render_template, url_for

import config
import core
from notify import DigestItem
from schedule_source import Match, OpenFootballSource
from store import score_day

app = Flask(__name__)


def _valid_date(date: str) -> bool:
    try:
        dt.date.fromisoformat(date)
        return True
    except ValueError:
        return False


def _pct(x: float) -> int:
    return round(x * 100)


def _item_view(item: DigestItem) -> dict:
    """Flatten a DigestItem into a template-friendly dict."""
    m = item.match
    base = {
        "team1": m.team1,
        "team2": m.team2,
        "group": m.group_label,
        "round": m.round,
        "has_pred": item.prediction is not None,
        "note": item.note,
    }
    if item.prediction is None:
        return base
    p = item.prediction
    base.update(
        {
            "p1": _pct(p.three_way.p1),
            "draw": _pct(p.three_way.draw),
            "p2": _pct(p.three_way.p2),
            "score1": p.scoreline[0],
            "score2": p.scoreline[1],
            "lambda1": round(p.lambda1, 2),
            "lambda2": round(p.lambda2, 2),
        }
    )
    if p.totals is not None:
        base["totals"] = {
            "line": p.totals.line,
            "over": _pct(p.totals.p_over),
            "under": _pct(p.totals.p_under),
        }
    return base


def _accuracy_for(date: str) -> Optional[dict]:
    """Score the given date's stored picks vs results, if any are scorable."""
    schedule = OpenFootballSource()
    try:
        finished = schedule.matches_on(date)
    except Exception:
        return None
    report = score_day(date, finished)
    if report.total == 0:
        return None
    return {
        "date": date,
        "exact": report.exact_count,
        "outcomes": report.outcome_count,
        "total": report.total,
        "picks": [
            {
                "label": p.label,
                "predicted": f"{p.predicted[0]}–{p.predicted[1]}",
                "actual": f"{p.actual[0]}–{p.actual[1]}",
                "exact": p.exact,
                "outcome_correct": p.outcome_correct,
            }
            for p in report.picks
        ],
    }


def _shift(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


@app.route("/")
def index():
    return redirect(url_for("day_view", date=dt.date.today().isoformat()))


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/day/<date>")
def day_view(date: str):
    if not _valid_date(date):
        abort(400, "date must be YYYY-MM-DD")
    error = None
    items: List[DigestItem] = []
    try:
        items = core.build_items(date)
    except Exception as exc:  # whole-day upstream failure
        error = f"Could not load data: {exc}"
    views = [_item_view(it) for it in items]
    accuracy = _accuracy_for(_shift(date, -1))
    return render_template(
        "day.html",
        date=date,
        prev_url=url_for("day_view", date=_shift(date, -1)),
        next_url=url_for("day_view", date=_shift(date, 1)),
        prev_label=_shift(date, -1),
        next_label=_shift(date, 1),
        items=views,
        accuracy=accuracy,
        error=error,
        demo=False,
    )


def _demo_items() -> List[DigestItem]:
    """Illustrative fixtures with plausible odds. Scorelines are still produced
    by the real Poisson model — only the input odds are hand-picked for the demo.
    """
    from predict import Totals, build_prediction

    # (team1, team2, group, p1, draw, p2, optional totals)
    specs = [
        ("Brazil", "Croatia", "Group C", 0.60, 0.25, 0.15, (2.5, 0.55, 0.45)),
        ("Argentina", "Mexico", "Group D", 0.52, 0.27, 0.21, None),
        ("France", "Senegal", "Group E", 0.58, 0.24, 0.18, (2.5, 0.61, 0.39)),
        ("England", "United States", "Group B", 0.55, 0.26, 0.19, None),
        ("Spain", "Germany", "Group F", 0.40, 0.28, 0.32, (2.5, 0.58, 0.42)),
        ("Portugal", "Netherlands", "Group A", 0.38, 0.27, 0.35, None),
    ]
    items: List[DigestItem] = []
    for t1, t2, grp, p1, dr, p2, tot in specs:
        totals = Totals(*tot) if tot else None
        m = Match(date="2026-06-15", team1=t1, team2=t2, group=grp)
        pred = build_prediction(t1, t2, p1, dr, p2, totals=totals)
        items.append(DigestItem(match=m, prediction=pred))
    return items


def _demo_accuracy() -> dict:
    return {
        "date": "matchday 2",
        "exact": 2,
        "outcomes": 4,
        "total": 5,
        "picks": [
            {"label": "Japan vs Spain", "predicted": "1–2", "actual": "1–2", "exact": True, "outcome_correct": True},
            {"label": "Morocco vs Belgium", "predicted": "1–1", "actual": "2–0", "exact": False, "outcome_correct": False},
            {"label": "Italy vs Canada", "predicted": "2–0", "actual": "2–0", "exact": True, "outcome_correct": True},
            {"label": "Colombia vs Egypt", "predicted": "1–0", "actual": "3–1", "exact": False, "outcome_correct": True},
            {"label": "Norway vs Ecuador", "predicted": "1–0", "actual": "1–0", "exact": True, "outcome_correct": True},
        ],
    }


@app.route("/demo")
def demo_view():
    views = [_item_view(it) for it in _demo_items()]
    return render_template(
        "day.html",
        date="Sample matchday",
        prev_url=url_for("index"),
        next_url=url_for("index"),
        prev_label="live",
        next_label="live",
        items=views,
        accuracy=_demo_accuracy(),
        error=None,
        demo=True,
    )


@app.route("/api/day/<date>")
def day_api(date: str):
    if not _valid_date(date):
        abort(400, "date must be YYYY-MM-DD")
    try:
        items = core.build_items(date)
    except Exception as exc:
        return jsonify({"date": date, "error": str(exc), "matches": []}), 502
    return jsonify({"date": date, "matches": [_item_view(it) for it in items]})


if __name__ == "__main__":
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=True)
