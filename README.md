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

## Telegram push (recommended)

Daily push of today's picks **and** yesterday's accuracy, straight to your
phone — no SMTP/deliverability hassle.

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token.
2. DM your new bot anything once.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and read your
   `chat.id` from the JSON.
4. Put both in `.env`:

   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

`main.py` pushes to Telegram whenever these are set (alongside email if SMTP is
also configured). Either channel alone is enough; with neither, a plain run
errors (use `--dry-run` to preview).

## Web dashboard

A live Flask dashboard renders the same data in the browser: one card per
match, win/draw/win probability bars, the model scoreline, over/under, and a
yesterday-accuracy panel.

```bash
.venv/bin/python webapp.py          # dev server on http://localhost:8000
```

Routes: `/` (today), `/day/<YYYY-MM-DD>`, `/api/day/<YYYY-MM-DD>` (JSON),
`/healthz`. Data is fetched live per request (no cache).

## Docker + VPS deploy (worldcup.botcloud.pro)

```bash
cp .env.example .env        # fill in TELEGRAM_* (and SMTP_* if you want email)
docker compose up -d --build
```

The container runs gunicorn on `127.0.0.1:8000` (localhost-only; nginx fronts
it). `predictions.jsonl` persists in the `./data` volume.

**nginx + TLS** (existing nginx on the host; does NOT touch `coda-defense.com`):

```bash
# DNS first: A record  worldcup.botcloud.pro -> <VPS IP>
sudo cp deploy/worldcup.botcloud.pro.nginx /etc/nginx/sites-available/worldcup.botcloud.pro
sudo ln -s /etc/nginx/sites-available/worldcup.botcloud.pro /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d worldcup.botcloud.pro
```

**Daily job** (host crontab, 15:00 Israel time, DST-safe):

```cron
CRON_TZ=Asia/Jerusalem
0 15 * * * docker exec worldcup-app sh -c 'python main.py --score "$(date -u -d yesterday +%F)" ; python main.py' >> /var/log/worldcup.log 2>&1
```

The job scores yesterday (Telegram push), then builds today (records to the
volume + Telegram push). The dashboard stays live and stateless; the job is
what persists predictions for next-day scoring.

## Hosting (alternative: GitHub Actions)

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
| `notify.py` | `render_digest()`, `send_email()` (SMTP/SSL), `send_telegram()`. |
| `store.py` | Append predictions to `predictions.jsonl`, score vs results. |
| `core.py` | `build_items(date)` — shared orchestration (CLI + web + job). |
| `main.py` | Orchestration + CLI (digest, scoring, Telegram/email push). |
| `webapp.py` | Flask dashboard (live per-request). |
| `templates/`, `static/` | Dashboard HTML + CSS. |
| `Dockerfile`, `docker-compose.yml` | Container + VPS deploy. |
| `deploy/` | nginx server block for `worldcup.botcloud.pro`. |
| `tests/` | Offline test suite (`python -m unittest discover -s tests`). |

The schedule source stays behind `ScheduleSource` so a paid provider
(worldcupapi.com, stable match_ids) can be dropped in without downstream change.

## Caveats / not-yet-verified

The Polymarket per-match market structure is unconfirmed until markets go live
(~June 10+). The client handles both a single 3-way market and a split
yes/no layout; once the real layout is confirmed, prune the dead branch. Team
name matching (`ALIASES` in `polymarket.py`) is a starter set — expect to extend
it. See in-code comments for the full verification checklist.
