"""Prediction persistence + scoring.

Predictions are appended to a JSONL file (one record per fixture per day).
The next day we score those records against real openfootball results.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

import config
from predict import Prediction
from schedule_source import Match


def record_from(date: str, match: Match, prediction: Prediction) -> dict:
    """Flatten one prediction into a JSON-serialisable record."""
    return {
        "date": date,
        "team1": match.team1,
        "team2": match.team2,
        "group": match.group,
        "scoreline": list(prediction.scoreline),
        "lambda1": prediction.lambda1,
        "lambda2": prediction.lambda2,
        "p1": prediction.three_way.p1,
        "draw": prediction.three_way.draw,
        "p2": prediction.three_way.p2,
    }


def append_predictions(records: List[dict], path: str = config.PREDICTIONS_PATH) -> None:
    """Append records to the JSONL store, creating it if needed."""
    if not records:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_predictions(date: str, path: str = config.PREDICTIONS_PATH) -> List[dict]:
    """Return all stored prediction records for a given date."""
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("date") == date:
                out.append(rec)
    return out


def _result_sign(h: int, a: int) -> str:
    if h > a:
        return "1"
    if h < a:
        return "2"
    return "X"


@dataclass
class ScoredPick:
    label: str
    predicted: Tuple[int, int]
    actual: Tuple[int, int]
    exact: bool  # predicted scoreline == actual scoreline
    outcome_correct: bool  # got the 1/X/2 right


@dataclass
class ScoreReport:
    date: str
    picks: List[ScoredPick]

    @property
    def exact_count(self) -> int:
        return sum(1 for p in self.picks if p.exact)

    @property
    def outcome_count(self) -> int:
        return sum(1 for p in self.picks if p.outcome_correct)

    @property
    def total(self) -> int:
        return len(self.picks)


def score_day(
    date: str,
    finished_matches: List[Match],
    path: str = config.PREDICTIONS_PATH,
) -> ScoreReport:
    """Score stored predictions for `date` against finished match results."""
    results: Dict[Tuple[str, str], Tuple[int, int]] = {}
    for m in finished_matches:
        if m.finished:
            results[(m.team1, m.team2)] = (m.score1, m.score2)

    picks: List[ScoredPick] = []
    for rec in load_predictions(date, path):
        key = (rec["team1"], rec["team2"])
        if key not in results:
            continue
        actual = results[key]
        predicted = (rec["scoreline"][0], rec["scoreline"][1])
        picks.append(
            ScoredPick(
                label=f"{rec['team1']} vs {rec['team2']}",
                predicted=predicted,
                actual=actual,
                exact=(predicted == actual),
                outcome_correct=(
                    _result_sign(*predicted) == _result_sign(*actual)
                ),
            )
        )
    return ScoreReport(date=date, picks=picks)
