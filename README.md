# World Cup daily score-prediction emailer

Once a day, for each 2026 FIFA World Cup match scheduled that day, this tool:

1. Fetches fixtures from the free [openfootball](https://github.com/openfootball)
   JSON feed.
2. Finds the matching market on Polymarket's public Gamma API.
3. Reports the crowd's implied probabilities (team1 win / draw / team2 win, plus
   over/under goals where available).
4. Derives a single most-likely scoreline from those odds with an
   independent-Poisson model.
5. Emails the digest. The next day it scores the previous day's picks against
   the real results.

## Honesty note (read this)

**Polymarket does not publish scorelines.** The probabilities are real market
data. The scoreline is *our own* Poisson model fitted to those odds — it is not
"Polymarket's prediction." All copy in the app keeps this distinction.

## Install

```bash
pip install -r requirements.txt
```

Dependency-light by design: Python stdlib + `requests`. No pandas/scipy/numpy —
the Poisson math is hand-rolled so it runs anywhere.

## Use

```bash
python main.py --dry-run            # today's picks, printed (no email)
python main.py --date 2026-06-15    # a specific day
python main.py                      # emailed (needs SMTP env vars)
python main.py --score 2026-06-14   # accuracy of that day's picks vs results
python main.py --date 2026-06-15 --debug   # verbose market-matching trace
```

`--debug` prints, per fixture, every Gamma event considered and why each was
accepted or rejected. Use it on the first live match day to fix name mismatches
fast.

## Email configuration (env only)

| Var | Meaning |
|-----|---------|
| `SMTP_HOST` | SMTP server, e.g. `smtp.gmail.com` |
| `SMTP_PORT` | SSL port, e.g. `465` |
| `SMTP_USER` | SMTP username |
| `SMTP_PASSWORD` | password / Gmail **App Password** |
| `EMAIL_FROM` | sender address (defaults to `SMTP_USER`) |
| `EMAIL_TO` | recipient address |

Secrets never live in the repo. Locally use env vars; hosted, use repo Secrets.

## Hosting

`.github/workflows/daily.yml` runs a daily GitHub Actions cron: it scores
yesterday, emails today, and commits the `predictions.jsonl` log so scoring
persists across runs. Set the same SMTP vars as repo Secrets.

## Architecture

| File | Role |
|------|------|
| `config.py` | All settings, env-driven. Secrets from env only. |
| `schedule_source.py` | `Match` + `ScheduleSource` interface + `OpenFootballSource`. Swap providers here. |
| `polymarket.py` | Gamma API client: `find_market(team1, team2)` → `MarketSnapshot`. |
| `predict.py` | Pure-stdlib Poisson model: `build_prediction(...)` → `Prediction`. |
| `notify.py` | `render_digest()` (text + HTML) and `send_email()` (SMTP/SSL). |
| `store.py` | Append predictions to `predictions.jsonl`, score vs results. |
| `main.py` | Orchestration + CLI. |

The schedule source stays behind `ScheduleSource` so a paid provider
(worldcupapi.com, stable match_ids) can be dropped in without downstream change.

## Caveats / not-yet-verified

The Polymarket per-match market structure is unconfirmed until markets go live
(~June 10+). The client handles both a single 3-way market and a split
yes/no layout; once the real layout is confirmed, prune the dead branch. Team
name matching (`ALIASES` in `polymarket.py`) is a starter set — expect to extend
it. See in-code comments for the full verification checklist.
