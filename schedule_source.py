"""Schedule providers behind a stable interface.

`ScheduleSource` is the seam: today it's openfootball (free JSON); later the
user can drop in worldcupapi.com (paid, stable match_ids) without changing any
downstream code. Everything downstream only touches `Match`.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol

import requests

import config


@dataclass(frozen=True)
class Match:
    """One fixture. `date` is the local match date (YYYY-MM-DD) as published.

    `kickoff_utc` is the parsed UTC instant when a time + offset was available,
    else None. `score1`/`score2` are filled only once the match is finished
    (used by the scoring path).
    """

    date: str
    team1: str
    team2: str
    group: str = ""
    round: str = ""
    ground: str = ""
    kickoff_utc: Optional[dt.datetime] = None
    score1: Optional[int] = None
    score2: Optional[int] = None

    @property
    def finished(self) -> bool:
        return self.score1 is not None and self.score2 is not None

    def label(self) -> str:
        return f"{self.team1} vs {self.team2}"


class ScheduleSource(Protocol):
    """Provider seam. Implementations fetch fixtures for a given date."""

    def matches_on(self, date: str) -> List[Match]:
        ...


# openfootball times look like "13:00 UTC-6" or "16:00 UTC+1" (sometimes just
# "13:00"). Capture HH:MM and an optional UTC offset in hours.
_TIME_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})(?:\s*UTC\s*([+-]\d{1,2})(?::(\d{2}))?)?\s*$"
)


def _parse_kickoff(date: str, time_str: str) -> Optional[dt.datetime]:
    """Combine a YYYY-MM-DD date and an openfootball time into a UTC instant.

    Returns None if the time is absent/unparseable — callers must tolerate it.
    """
    if not time_str:
        return None
    m = _TIME_RE.match(time_str)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        return None
    naive = dt.datetime(d.year, d.month, d.day, hour, minute)
    off_h = m.group(3)
    if off_h is None:
        # No offset published: treat the wall-clock time as UTC.
        return naive.replace(tzinfo=dt.timezone.utc)
    off_min = int(m.group(4) or 0)
    sign = 1 if int(off_h) >= 0 else -1
    delta = dt.timedelta(hours=abs(int(off_h)), minutes=off_min) * sign
    local = naive.replace(tzinfo=dt.timezone(delta))
    return local.astimezone(dt.timezone.utc)


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class OpenFootballSource:
    """Fetches the full tournament JSON once, then filters by date.

    The feed is small (the whole cup), so a single GET per run is fine. We
    cache it on the instance to keep `matches_on` cheap if called repeatedly.
    """

    def __init__(self, url: str = config.OPENFOOTBALL_URL, session: Optional[requests.Session] = None):
        self.url = url
        self._session = session or requests.Session()
        self._cache: Optional[List[Match]] = None

    def _fetch_all(self) -> List[Match]:
        if self._cache is not None:
            return self._cache
        resp = self._session.get(
            self.url,
            timeout=config.HTTP_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        out: List[Match] = []
        for raw in data.get("matches", []):
            date = (raw.get("date") or "").strip()
            team1 = (raw.get("team1") or "").strip()
            team2 = (raw.get("team2") or "").strip()
            if not (date and team1 and team2):
                continue
            out.append(
                Match(
                    date=date,
                    team1=team1,
                    team2=team2,
                    group=(raw.get("group") or "").strip(),
                    round=(raw.get("round") or "").strip(),
                    ground=(raw.get("ground") or "").strip(),
                    kickoff_utc=_parse_kickoff(date, (raw.get("time") or "").strip()),
                    score1=_to_int(raw.get("score1")),
                    score2=_to_int(raw.get("score2")),
                )
            )
        self._cache = out
        return out

    def matches_on(self, date: str) -> List[Match]:
        return [m for m in self._fetch_all() if m.date == date]
