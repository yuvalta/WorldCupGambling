"""Pure-stdlib independent-Poisson model.

IMPORTANT framing: Polymarket does not publish scorelines. The probabilities
this module consumes are real market data. The scoreline it produces is OUR
model's fit to those odds — never present it as the market's prediction.

Method:
  1. De-vig the 3-way moneyline so P(team1) + P(draw) + P(team2) == 1.
  2. Fit (lambda1, lambda2) — the expected goals for each side — by grid
     search so an independent double-Poisson reproduces the de-vigged 3-way
     probabilities, and the over/under totals probability when available.
  3. Read off the single most-likely scoreline from the fitted Poissons.

No numpy/scipy: factorials and exp via the stdlib `math` module, sums over a
truncated goal grid (config.MAX_GOALS).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config


@dataclass
class ThreeWay:
    """De-vigged moneyline probabilities. Sum to 1 by construction."""

    p1: float  # team1 win
    draw: float
    p2: float  # team2 win


@dataclass
class Totals:
    """Over/under on total goals, e.g. line=2.5, p_over=0.55."""

    line: float
    p_over: float
    p_under: float


@dataclass
class Prediction:
    team1: str
    team2: str
    three_way: ThreeWay
    lambda1: float
    lambda2: float
    scoreline: Tuple[int, int]
    totals: Optional[Totals] = None
    # Top alternative scorelines (score -> prob), for transparency.
    top_scores: List[Tuple[Tuple[int, int], float]] = field(default_factory=list)
    fit_error: float = 0.0
    # "market" when the scoreline comes from Polymarket's Exact Score market,
    # "model" when it's the fitted-Poisson fallback. Display honestly.
    scoreline_source: str = "model"


def devig_three_way(p1: float, draw: float, p2: float) -> ThreeWay:
    """Normalise three raw market probabilities to sum to 1.

    Markets price in a margin (vig), so raw implied probabilities sum to >1.
    Proportional de-vigging is the standard, assumption-light correction.
    """
    total = p1 + draw + p2
    if total <= 0:
        raise ValueError("three-way probabilities must be positive")
    return ThreeWay(p1=p1 / total, draw=draw / total, p2=p2 / total)


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def _pmf_vector(lam: float, max_goals: int) -> List[float]:
    """PMF for 0..max_goals with the tail mass folded into the last bucket."""
    vec = [_poisson_pmf(k, lam) for k in range(max_goals + 1)]
    tail = max(0.0, 1.0 - sum(vec))
    vec[-1] += tail
    return vec


def _model_outcomes(lam1: float, lam2: float, max_goals: int) -> Tuple[float, float, float]:
    """Return (P team1 win, P draw, P team2 win) for independent Poissons."""
    v1 = _pmf_vector(lam1, max_goals)
    v2 = _pmf_vector(lam2, max_goals)
    p1 = pd = p2 = 0.0
    for i, a in enumerate(v1):
        for j, b in enumerate(v2):
            joint = a * b
            if i > j:
                p1 += joint
            elif i == j:
                pd += joint
            else:
                p2 += joint
    return p1, pd, p2


def _model_over(lam1: float, lam2: float, line: float, max_goals: int) -> float:
    """P(total goals > line) for independent Poissons."""
    v1 = _pmf_vector(lam1, max_goals)
    v2 = _pmf_vector(lam2, max_goals)
    p_over = 0.0
    for i, a in enumerate(v1):
        for j, b in enumerate(v2):
            if (i + j) > line:
                p_over += a * b
    return p_over


def _fit_lambdas(
    tw: ThreeWay,
    totals: Optional[Totals],
    max_goals: int,
    lam_max: float,
    lam_step: float,
) -> Tuple[float, float, float]:
    """Grid-search (lambda1, lambda2) minimising squared error vs the market.

    Error = squared diff on the 3-way probabilities, plus (if present) a
    weighted squared diff on P(over). Returns (lam1, lam2, error).
    """
    steps = int(round(lam_max / lam_step))
    grid = [round((i + 1) * lam_step, 4) for i in range(steps)]  # skip 0
    best = (1.2, 1.2, float("inf"))
    totals_weight = 0.5  # 3-way is the primary signal
    for lam1 in grid:
        for lam2 in grid:
            mp1, mpd, mp2 = _model_outcomes(lam1, lam2, max_goals)
            err = (mp1 - tw.p1) ** 2 + (mpd - tw.draw) ** 2 + (mp2 - tw.p2) ** 2
            if totals is not None:
                mo = _model_over(lam1, lam2, totals.line, max_goals)
                err += totals_weight * (mo - totals.p_over) ** 2
            if err < best[2]:
                best = (lam1, lam2, err)
    return best


def _all_scores(lam1: float, lam2: float, max_goals: int) -> List[Tuple[Tuple[int, int], float]]:
    v1 = _pmf_vector(lam1, max_goals)
    v2 = _pmf_vector(lam2, max_goals)
    scores: List[Tuple[Tuple[int, int], float]] = []
    for i, a in enumerate(v1):
        for j, b in enumerate(v2):
            scores.append(((i, j), a * b))
    scores.sort(key=lambda kv: kv[1], reverse=True)
    return scores


def _most_likely_scoreline(lam1: float, lam2: float, max_goals: int,
                            outcome: str, top_n: int = 5):
    """Most-likely scoreline CONSISTENT with the predicted match outcome.

    The raw joint argmax of two independent Poissons is (mode lam1, mode lam2)
    — almost always 0-0/1-0/0-1, and it structurally hides draws (draw mass is
    spread across 0-0,1-1,2-2 so no single tie ever wins outright). That makes
    the headline outcome and the displayed score disagree ("team1 favoured" but
    score 0-1) and ties practically never appear.

    Instead, condition on the modal outcome ("1"=team1 win, "X"=draw, "2"=team2
    win) and return the most-likely score within that class. The score then
    always matches the predicted winner, and draws surface when draw is modal.
    """
    scores = _all_scores(lam1, lam2, max_goals)

    def in_class(i: int, j: int) -> bool:
        return (outcome == "1" and i > j) or (outcome == "X" and i == j) or (
            outcome == "2" and i < j)

    for (i, j), p in scores:
        if in_class(i, j):
            return (i, j), scores[:top_n]
    return scores[0][0], scores[:top_n]  # fallback (shouldn't happen)


def build_prediction(
    team1: str,
    team2: str,
    p1: float,
    draw: float,
    p2: float,
    totals: Optional[Totals] = None,
    exact_scores: Optional[List[Tuple[Tuple[int, int], float]]] = None,
    max_goals: int = config.MAX_GOALS,
    lam_max: float = config.LAMBDA_MAX,
    lam_step: float = config.LAMBDA_STEP,
) -> Prediction:
    """Build a Prediction from raw (vigged) market probabilities.

    If `exact_scores` (crowd-implied scoreline probabilities from Polymarket's
    Exact Score market) is supplied, the headline scoreline is read straight
    from that market — real data beats our model. The Poisson is still fitted
    (for lambdas/totals) and serves as the fallback when no exact-score market
    exists.
    """
    tw = devig_three_way(p1, draw, p2)
    lam1, lam2, err = _fit_lambdas(tw, totals, max_goals, lam_max, lam_step)
    if exact_scores:
        scoreline = exact_scores[0][0]
        top = list(exact_scores[:5])
        source = "market"
    else:
        outcome = max((("1", tw.p1), ("X", tw.draw), ("2", tw.p2)), key=lambda kv: kv[1])[0]
        scoreline, top = _most_likely_scoreline(lam1, lam2, max_goals, outcome)
        source = "model"
    return Prediction(
        team1=team1,
        team2=team2,
        three_way=tw,
        lambda1=lam1,
        lambda2=lam2,
        scoreline=scoreline,
        totals=totals,
        top_scores=top,
        fit_error=err,
        scoreline_source=source,
    )
