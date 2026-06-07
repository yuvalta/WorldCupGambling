"""Shared orchestration: turn a date into per-match prediction items.

Single source of truth used by the CLI (`main.py`), the web app
(`webapp.py`), and the daily job. One function, one job. Per-match failures are
isolated so one bad fixture or market never aborts the whole day.
"""

from __future__ import annotations

from typing import List, Optional

from notify import DigestItem
from polymarket import PolymarketClient
from predict import Totals, build_prediction
from schedule_source import OpenFootballSource, ScheduleSource


def build_items(
    date: str,
    schedule: Optional[ScheduleSource] = None,
    market_client: Optional[PolymarketClient] = None,
    debug: bool = False,
) -> List[DigestItem]:
    """Fetch fixtures for `date`, attach market snapshot + Poisson prediction.

    `schedule` / `market_client` are injectable for tests; defaults hit the
    live providers. Each fixture is wrapped: any error becomes a DigestItem
    with a `note` rather than blowing up the run.
    """
    schedule = schedule or OpenFootballSource()
    market_client = market_client or PolymarketClient()

    matches = schedule.matches_on(date)
    items: List[DigestItem] = []
    for m in matches:
        try:
            snap = market_client.find_market(m.team1, m.team2, debug=debug)
        except Exception as exc:  # network/parse error for one fixture
            items.append(DigestItem(match=m, prediction=None, note=f"market lookup failed: {exc}"))
            continue
        if snap is None:
            items.append(DigestItem(match=m, prediction=None, note="no live market found"))
            continue
        totals = None
        if snap.line is not None and snap.p_over is not None:
            totals = Totals(
                line=snap.line,
                p_over=snap.p_over,
                p_under=snap.p_under if snap.p_under is not None else (1 - snap.p_over),
            )
        try:
            prediction = build_prediction(
                m.team1, m.team2, snap.p1, snap.draw, snap.p2, totals=totals
            )
        except Exception as exc:
            items.append(DigestItem(match=m, prediction=None, note=f"model failed: {exc}"))
            continue
        items.append(DigestItem(match=m, prediction=prediction))
    return items
