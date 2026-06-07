"""Polymarket Gamma API client — the fuzzy, highest-risk module.

`find_market(team1, team2)` searches Gamma events for the match and returns a
`MarketSnapshot` with the crowd's implied probabilities. Returns None when no
confident match exists (caller treats that as "no market yet").

Two market layouts are handled because which one Polymarket uses for WC
matches is UNVERIFIED until a live match day:
  A) a single 3-way market with outcomes ["Brazil","Draw","Croatia"]
  B) a split layout: separate "Will <team> win?" yes/no markets (+ a draw mkt)
Once confirmed live, prune the dead branch (see handoff next-steps #3).

Gotchas baked in:
  - `outcomes` / `outcomePrices` arrive as JSON-encoded STRINGS → `_jload`.
  - Gamma reports stale markets as active:true, closed:false. We trust
    `closed` and `acceptingOrders` instead (Polymarket/rs-clob-client#199).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

import config

# Map openfootball country names to the spellings/forms Polymarket tends to
# use. Starter set per handoff — expect to extend on the first live day.
ALIASES: Dict[str, List[str]] = {
    "South Korea": ["Korea", "Korea Republic", "Republic of Korea"],
    "North Korea": ["Korea DPR", "DPR Korea"],
    "United States": ["USA", "United States", "US", "U.S.A."],
    "Czech Republic": ["Czechia"],
    "Ivory Coast": ["Côte d'Ivoire", "Cote d'Ivoire"],
    "Iran": ["IR Iran"],
    "Cape Verde": ["Cabo Verde"],
    "Bosnia and Herzegovina": ["Bosnia", "Bosnia-Herzegovina"],
}


@dataclass
class MarketSnapshot:
    """Crowd-implied probabilities for one fixture. Probs are raw (vigged);
    de-vigging happens in the prediction step."""

    team1: str
    team2: str
    p1: float  # implied P(team1 win)
    draw: float  # implied P(draw)
    p2: float  # implied P(team2 win)
    line: Optional[float] = None  # over/under goals line, e.g. 2.5
    p_over: Optional[float] = None
    p_under: Optional[float] = None
    event_title: str = ""
    event_id: str = ""
    clob_token_ids: List[str] = field(default_factory=list)
    layout: str = ""  # "three_way" | "split" — which branch produced this


def _jload(value):
    """Gamma encodes list fields as JSON strings. Decode defensively."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _name_forms(team: str) -> List[str]:
    forms = [team] + ALIASES.get(team, [])
    return [_norm(f) for f in forms if f]


def _title_mentions(title: str, team: str) -> bool:
    nt = _norm(title)
    return any(form and form in nt for form in _name_forms(team))


# Over/under question phrasing varies. Pull the first decimal/half line we see.
# Tighten to the real phrasing once confirmed live (handoff next-steps #3).
_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:goals?|total)?", re.IGNORECASE)


def _extract_line(question: str) -> Optional[float]:
    """Best-effort extraction of the goals line from a totals question."""
    if not question:
        return None
    # Prefer an explicit "x.5" half-line if present (typical for totals).
    half = re.search(r"(\d+\.5)", question)
    if half:
        return float(half.group(1))
    m = _LINE_RE.search(question)
    return float(m.group(1)) if m else None


def _market_is_live(market: dict) -> bool:
    """Keep only tradeable markets. Trust closed/acceptingOrders, not active."""
    if market.get("closed"):
        return False
    # acceptingOrders may be absent on older payloads; absence != closed.
    if market.get("acceptingOrders") is False:
        return False
    return True


def _outcome_prices(market: dict) -> Optional[Dict[str, float]]:
    """Map normalised outcome label -> price for one market."""
    outcomes = _jload(market.get("outcomes"))
    prices = _jload(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    try:
        return {str(o).strip().lower(): float(p) for o, p in zip(outcomes, prices)}
    except (TypeError, ValueError):
        return None


class PolymarketClient:
    def __init__(self, base: str = config.GAMMA_BASE, session: Optional[requests.Session] = None):
        self.base = base.rstrip("/")
        self._session = session or requests.Session()

    def _get_events(self, query: str) -> List[dict]:
        """Search Gamma events. Returns raw event dicts (each may nest markets)."""
        params = {
            "search": query,
            "closed": "false",
            "limit": "40",
        }
        resp = self._session.get(
            f"{self.base}/events",
            params=params,
            timeout=config.HTTP_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        # Gamma may return a bare list or {"data": [...]} depending on endpoint.
        if isinstance(data, dict):
            return data.get("data", []) or []
        return data or []

    def _candidate_events(self, team1: str, team2: str) -> List[dict]:
        """Collect events whose title mentions BOTH teams (any alias form)."""
        seen: Dict[str, dict] = {}
        for q in (f"{team1} {team2}", team1, team2):
            for ev in self._get_events(q):
                ev_id = str(ev.get("id") or ev.get("slug") or id(ev))
                seen.setdefault(ev_id, ev)
        out = []
        for ev in seen.values():
            title = ev.get("title") or ev.get("question") or ""
            if _title_mentions(title, team1) and _title_mentions(title, team2):
                out.append(ev)
        return out

    # --- layout parsers ----------------------------------------------------

    def _parse_three_way(self, event: dict, team1: str, team2: str) -> Optional[MarketSnapshot]:
        """Layout A: one market, outcomes like ["Brazil","Draw","Croatia"]."""
        for market in event.get("markets", []) or []:
            if not _market_is_live(market):
                continue
            prices = _outcome_prices(market)
            if not prices or len(prices) != 3:
                continue
            if "draw" not in prices and "tie" not in prices:
                continue
            p_draw = prices.get("draw", prices.get("tie", 0.0))
            p1 = p2 = None
            for label, price in prices.items():
                if label in ("draw", "tie"):
                    continue
                if any(form in _norm(label) for form in _name_forms(team1)):
                    p1 = price
                elif any(form in _norm(label) for form in _name_forms(team2)):
                    p2 = price
            if p1 is None or p2 is None:
                continue
            return MarketSnapshot(
                team1=team1,
                team2=team2,
                p1=p1,
                draw=p_draw,
                p2=p2,
                event_title=event.get("title", ""),
                event_id=str(event.get("id", "")),
                clob_token_ids=_jload(market.get("clobTokenIds")) or [],
                layout="three_way",
            )
        return None

    def _parse_split(self, event: dict, team1: str, team2: str) -> Optional[MarketSnapshot]:
        """Layout B: separate "Will <team> win?" yes/no markets (+ draw)."""
        p1 = p2 = p_draw = None
        token_ids: List[str] = []
        for market in event.get("markets", []) or []:
            if not _market_is_live(market):
                continue
            q = (market.get("question") or "").strip()
            prices = _outcome_prices(market)
            if not prices:
                continue
            yes = prices.get("yes")
            if yes is None:
                continue
            nq = _norm(q)
            if any(form in nq for form in _name_forms(team1)):
                p1 = yes
            elif any(form in nq for form in _name_forms(team2)):
                p2 = yes
            elif "draw" in nq or "tie" in nq:
                p_draw = yes
            token_ids += _jload(market.get("clobTokenIds")) or []
        if p1 is None or p2 is None:
            return None
        if p_draw is None:
            # No explicit draw market: infer residual, floored at 0.
            p_draw = max(0.0, 1.0 - p1 - p2)
        return MarketSnapshot(
            team1=team1,
            team2=team2,
            p1=p1,
            draw=p_draw,
            p2=p2,
            event_title=event.get("title", ""),
            event_id=str(event.get("id", "")),
            clob_token_ids=token_ids,
            layout="split",
        )

    def _attach_totals(self, event: dict, snap: MarketSnapshot) -> None:
        """Look for an over/under goals market within the same event."""
        for market in event.get("markets", []) or []:
            if not _market_is_live(market):
                continue
            q = (market.get("question") or "").lower()
            if "over" not in q and "under" not in q and "total" not in q:
                continue
            prices = _outcome_prices(market)
            if not prices:
                continue
            over = prices.get("over") or prices.get("yes")
            under = prices.get("under") or prices.get("no")
            line = _extract_line(market.get("question") or "")
            if over is None or line is None:
                continue
            snap.line = line
            snap.p_over = over
            snap.p_under = under if under is not None else max(0.0, 1.0 - over)
            return

    # --- public API --------------------------------------------------------

    def find_market(
        self, team1: str, team2: str, debug: bool = False
    ) -> Optional[MarketSnapshot]:
        """Return a MarketSnapshot for the fixture, or None if no match.

        When `debug`, prints every event considered and why it was rejected —
        the fast path for the first live debugging session.
        """
        events = self._candidate_events(team1, team2)
        if debug:
            print(f"  [debug] {team1} vs {team2}: {len(events)} candidate event(s)")
        for ev in events:
            title = ev.get("title", "")
            snap = self._parse_three_way(ev, team1, team2)
            if snap is None:
                snap = self._parse_split(ev, team1, team2)
            if snap is None:
                if debug:
                    print(f"  [debug]   rejected '{title}': no usable moneyline market")
                continue
            self._attach_totals(ev, snap)
            if debug:
                print(
                    f"  [debug]   matched '{title}' via {snap.layout} "
                    f"(p1={snap.p1:.2f} draw={snap.draw:.2f} p2={snap.p2:.2f})"
                )
            return snap
        return None


def find_market(team1: str, team2: str, debug: bool = False) -> Optional[MarketSnapshot]:
    """Module-level convenience wrapper."""
    return PolymarketClient().find_market(team1, team2, debug=debug)
