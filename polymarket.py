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

# Synonym groups: every spelling the SCHEDULE (openfootball) or POLYMARKET might
# use for the same country. Bidirectional — a fixture named with any form in a
# group matches a market named with any other. Polymarket is even inconsistent
# WITHIN one event (e.g. title "Bosnia-Herzegovina" but question "Bosnia and
# Herzegovina"), so list all spellings. Avoid ultra-short forms like "US" —
# they substring-collide ("us" is inside "australia"). Extend as new ones appear.
SYNONYM_GROUPS: List[List[str]] = [
    ["South Korea", "Korea", "Korea Republic", "Republic of Korea"],
    ["North Korea", "Korea DPR", "DPR Korea"],
    ["United States", "USA", "U.S.A.", "United States of America"],
    ["Czech Republic", "Czechia"],
    ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
    ["Iran", "IR Iran"],
    ["Cape Verde", "Cabo Verde"],
    ["Bosnia and Herzegovina", "Bosnia & Herzegovina", "Bosnia-Herzegovina", "Bosnia"],
    ["Turkey", "Türkiye", "Turkiye"],
]


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
    # Crowd-implied EXACT scorelines from Polymarket's "Exact Score" market,
    # as [((team1_goals, team2_goals), prob), ...] sorted high→low. Empty when
    # the market isn't offered. This is real data — preferred over the model's
    # Poisson scoreline when present.
    exact_scores: List[Tuple[Tuple[int, int], float]] = field(default_factory=list)


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


# normalized name -> all normalized forms in its synonym group (built once).
_FORMS: Dict[str, List[str]] = {}
for _group in SYNONYM_GROUPS:
    _normed = [_norm(x) for x in _group if x]
    for _n in _normed:
        _FORMS[_n] = _normed


def _name_forms(team: str) -> List[str]:
    """All normalized spellings equivalent to `team` (itself if no group)."""
    n = _norm(team)
    return _FORMS.get(n, [n])


def _title_mentions(title: str, team: str) -> bool:
    nt = _norm(title)
    return any(form and form in nt for form in _name_forms(team))


# Over/under question phrasing varies. Pull the first decimal/half line we see.
# Tighten to the real phrasing once confirmed live (handoff next-steps #3).
_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:goals?|total)?", re.IGNORECASE)

# "Exact Score: Brazil 1 - 0 Morocco?" → captures the two goal counts.
_SCORE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")


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
    # The whole World Cup slate is fetched once via the tag endpoint and cached.
    # Keyword search can NOT be trusted to surface a fixture: Gamma matches on
    # Polymarket's OWN spelling, so a schedule name like "South Korea" /
    # "Czech Republic" never finds the event titled "Korea Republic vs. Czechia"
    # (and /events?search= is ignored entirely, returning a generic firehose).
    # Pulling every WC event and matching locally by alias is the only reliable
    # approach. Verified live 2026-06: tag returns all 64 fixtures.
    _WC_TAG = "fifa-world-cup"

    def __init__(self, base: str = config.GAMMA_BASE, session: Optional[requests.Session] = None):
        self.base = base.rstrip("/")
        self._session = session or requests.Session()
        self._wc_cache: Optional[List[dict]] = None

    def _wc_events(self) -> List[dict]:
        """All open World Cup events (cached). Paginated: Gamma caps at 100/page."""
        if self._wc_cache is not None:
            return self._wc_cache
        out: List[dict] = []
        offset = 0
        while True:
            resp = self._session.get(
                f"{self.base}/events",
                params={
                    "tag_slug": self._WC_TAG,
                    "closed": "false",
                    "limit": "100",
                    "offset": str(offset),
                },
                timeout=config.HTTP_TIMEOUT,
                headers={"User-Agent": config.USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data if isinstance(data, list) else (data.get("data", []) or [])
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            if offset > 1000:  # safety bound; WC slate is ~530 events
                break
        self._wc_cache = out
        return out

    def _candidate_events(self, team1: str, team2: str) -> List[dict]:
        """WC events whose title mentions BOTH teams (any alias form)."""
        out = []
        for ev in self._wc_events():
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
            # Check draw FIRST: the draw question is phrased "Will <t1> vs. <t2>
            # end in a draw?" and therefore CONTAINS both team names — if we test
            # team1/team2 first it steals the draw market and overwrites p1/p2.
            # Team markets are "Will <team> win?" so also require "win" to match.
            if "draw" in nq or "tie" in nq:
                p_draw = yes
            elif "win" in nq and any(form in nq for form in _name_forms(team1)):
                p1 = yes
            elif "win" in nq and any(form in nq for form in _name_forms(team2)):
                p2 = yes
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

    def _attach_totals(self, events: List[dict], snap: MarketSnapshot) -> None:
        """Attach the full-match over/under goals line (prefers 2.5).

        The O/U markets live in a sibling "<T1> vs <T2> - More Markets" event,
        not the moneyline event, and are phrased "<T1> vs <T2>: O/U 2.5" with
        Over/Under outcomes. Scan ALL candidate events; skip half-time and
        team-specific O/U variants (their text after the colon names a team or
        a half rather than starting with the line)."""
        best: Optional[Tuple[float, float, float, Optional[float]]] = None
        for event in events:
            for market in event.get("markets", []) or []:
                if not _market_is_live(market):
                    continue
                q = (market.get("question") or "")
                ql = q.lower()
                if "half" in ql:
                    continue
                tail = ql.split(":")[-1].strip()
                if not (tail.startswith("o/u") or tail.startswith("over")):
                    continue
                prices = _outcome_prices(market)
                if not prices:
                    continue
                over = prices.get("over") or prices.get("yes")
                under = prices.get("under") or prices.get("no")
                line = _extract_line(q)
                if over is None or line is None:
                    continue
                closeness = abs(line - 2.5)  # 2.5 is the most informative line
                if best is None or closeness < best[0]:
                    best = (closeness, line, over, under)
        if best is not None:
            _, line, over, under = best
            snap.line = line
            snap.p_over = over
            snap.p_under = under if under is not None else max(0.0, 1.0 - over)

    def _attach_exact_scores(self, events: List[dict], snap: MarketSnapshot) -> None:
        """Read crowd-implied exact scorelines from the "- Exact Score" event.

        Markets are "Exact Score: <NameA> i - j <NameB>?" with Yes/No outcomes;
        the Yes price is P(that exact score). Orient (i, j) to (team1, team2) by
        which name precedes the score, so it's correct even if Polymarket lists
        the teams in the opposite order to the schedule. "Any Other Score" (the
        tail bucket) is skipped."""
        for event in events:
            if "exact score" not in (event.get("title") or "").lower():
                continue
            scores: Dict[Tuple[int, int], float] = {}
            for market in event.get("markets", []) or []:
                if not _market_is_live(market):
                    continue
                q = market.get("question") or ""
                if "any other" in q.lower():
                    continue
                m = _SCORE_RE.search(q)
                if not m:
                    continue
                prices = _outcome_prices(market)
                yes = prices.get("yes") if prices else None
                if yes is None:
                    continue
                a, b = int(m.group(1)), int(m.group(2))
                before = _norm(q[: m.start()])
                # If the name before the score is team2 (not team1), flip.
                if (any(f in before for f in _name_forms(snap.team2))
                        and not any(f in before for f in _name_forms(snap.team1))):
                    a, b = b, a
                key = (a, b)
                scores[key] = max(yes, scores.get(key, 0.0))
            if scores:
                snap.exact_scores = sorted(
                    scores.items(), key=lambda kv: kv[1], reverse=True
                )
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
            # Totals and exact scores live in sibling events — scan all candidates.
            self._attach_totals(events, snap)
            self._attach_exact_scores(events, snap)
            if debug:
                top = snap.exact_scores[0] if snap.exact_scores else None
                print(
                    f"  [debug]   matched '{title}' via {snap.layout} "
                    f"(p1={snap.p1:.2f} draw={snap.draw:.2f} p2={snap.p2:.2f}) "
                    f"totals={'%.1f' % snap.line if snap.line else 'N'} "
                    f"exact_top={top[0] if top else 'N'}"
                )
            return snap
        return None


def find_market(team1: str, team2: str, debug: bool = False) -> Optional[MarketSnapshot]:
    """Module-level convenience wrapper."""
    return PolymarketClient().find_market(team1, team2, debug=debug)
