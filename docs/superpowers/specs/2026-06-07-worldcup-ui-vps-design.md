# Design: World Cup UI + VPS deployment + Telegram push

Date: 2026-06-07
Status: Approved (pending written-spec review)

## Goal

Extend the existing World Cup score-prediction tool with:
1. A web dashboard showing each day's market probabilities and our model
   scorelines, served at `https://worldcup.botcloud.pro`.
2. A daily Telegram push containing today's picks **and** yesterday's accuracy.
3. Deployment on the user's VPS via Docker, fronted by the existing nginx.

The original CLI/email flow stays intact.

## Decisions (locked)

| Topic | Choice |
|-------|--------|
| UI | Live Flask app (fetches per request) |
| Notification | Telegram bot push (today's picks + yesterday's accuracy) |
| Domain | `worldcup.botcloud.pro` subdomain, HTTPS |
| TLS / proxy | Existing nginx + certbot (Let's Encrypt) |
| Process | Docker container (`docker compose`) |
| Data freshness | Live fetch every request (no UI cache) |
| Cron time | 15:00 Israel time (`CRON_TZ=Asia/Jerusalem`) |

**Relaxed constraint:** the original "stdlib + requests only" rule is relaxed —
Flask + gunicorn are now allowed (explicit user choice). The core domain logic
(`predict.py`, `polymarket.py`, `schedule_source.py`) stays stdlib + requests.

**Do NOT touch** the `coda-defense.com` domain or its nginx config.

## Architecture

```
 phone/browser ──HTTPS──> nginx (host, worldcup.botcloud.pro, certbot TLS)
                                   │ proxy_pass 127.0.0.1:8000
                                   ▼
                          Docker container
                          gunicorn ──> webapp.py (Flask)
                                          │ uses
                                          ▼
                                       core.py ──> schedule_source + polymarket + predict
                                          │ live fetch per request
                                          ▼
                               openfootball JSON + Polymarket Gamma

 host cron (CRON_TZ=Asia/Jerusalem, 15:00) ──> docker exec <container> python main.py ...
        ├─ score yesterday  ──> Telegram push (accuracy)
        └─ build today      ──> append predictions.jsonl (volume) ──> Telegram push (picks)
```

## Components

### New files

**`core.py`** — shared orchestration.
- `build_items(date, schedule, market_client, debug=False) -> List[DigestItem]`
  moved here from `main.py` (currently lives in `main.py`). Single source of
  truth used by web, CLI, and the daily job.
- `DigestItem` stays defined in `notify.py`; `core` imports it. One function,
  one job: turn a date into per-match prediction items.
- Each per-match step wrapped so one bad fixture/market does not abort the day;
  failures become a `DigestItem` with `note` set.

**`webapp.py`** — Flask app (`app`).
- `GET /` → redirect/render today (server's date).
- `GET /day/<date>` → dashboard for that date (`YYYY-MM-DD`).
- `GET /api/day/<date>` → JSON of the same data (for debugging / future use).
- `GET /healthz` → `200 "ok"` for container health checks.
- Live fetch per request via `core.build_items`. Wrapped in try/except with
  `HTTP_TIMEOUT`; on upstream failure renders a friendly "data unavailable"
  state, never a 500.
- Accuracy panel: reads `predictions.jsonl` + results for past dates via
  `store.score_day`.

**`templates/` + `static/`** — dashboard UI.
- `templates/day.html`: header (date + prev/next nav), one card per match:
  - team names + group/round
  - three probability bars (team1 win / draw / team2 win), de-vigged %
  - predicted-scoreline badge with xG (λ1–λ2)
  - over/under row when available
  - per-match "data unavailable" state
- Accuracy panel for the previous day: exact N/total, outcomes N/total, list.
- Footer honesty line: probabilities = Polymarket crowd odds; scoreline = our
  independent-Poisson model, not a Polymarket prediction.
- `static/style.css`: clean responsive layout, dark theme, no JS framework
  (vanilla; prev/next are links, bars are CSS widths).

**`Dockerfile`** — `python:3.12-slim`, install `requirements.txt`, run
`gunicorn -w 2 -b 0.0.0.0:8000 webapp:app`.

**`docker-compose.yml`** — one service `app`:
- `container_name: worldcup-app` (so the cron `docker exec worldcup-app ...`
  has a stable name)
- build `.`, `restart: unless-stopped`
- `ports: "127.0.0.1:8000:8000"` (bind localhost only; nginx fronts it)
- `env_file: .env` (SMTP optional, Telegram, OPENFOOTBALL_URL override)
- volume `./data:/app/data` for `predictions.jsonl` (PREDICTIONS_PATH points
  into `/app/data`)

**`.dockerignore`** — exclude `.git`, `__pycache__`, `data/`, docs.

**`deploy/worldcup.botcloud.pro.nginx`** — sample nginx server block
(proxy_pass to `127.0.0.1:8000`), plus README steps for `certbot --nginx -d
worldcup.botcloud.pro`. A separate file so it never collides with existing
site configs.

### Edited files

**`config.py`**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (env only).
- `WEB_HOST` / `WEB_PORT` (defaults `0.0.0.0` / `8000`).
- `PREDICTIONS_PATH` default → `data/predictions.jsonl`.
- `load_telegram_config()` → small dataclass with `.configured`.

**`notify.py`**
- `send_telegram(cfg, text)`: POST to
  `https://api.telegram.org/bot<token>/sendMessage` (requests). Uses the
  existing text digest body (Telegram-friendly, no HTML required; optional
  `parse_mode` left off for safety). Raises if not configured; callers gate on
  `.configured`.

**`main.py`**
- `build_items` import moves to `core`.
- Daily run: after building today's digest, also push to Telegram when
  configured (alongside existing email path; email remains optional).
- `--score <date>`: after scoring, push the accuracy summary to Telegram when
  configured.
- New flags: `--telegram` (force push even in otherwise dry contexts) and the
  existing `--dry-run` continues to print only.
- Send failures (SMTP or Telegram) are caught and logged; the job still exits 0
  so cron + jsonl persistence are not lost.

**`requirements.txt`** — add `flask>=3,<4`, `gunicorn>=21,<23`.

**`README.md`** — add UI + Docker + Telegram + nginx deploy sections.

## Data flow

1. **UI (live, stateless):** browser → nginx → gunicorn → Flask →
   `core.build_items(date)` → live fetch → render. No cache, no writes.
2. **Daily job (stateful):** host cron (15:00 Israel) → `docker exec app`:
   - `python main.py --score <yesterday>` → Telegram accuracy push.
   - `python main.py` → append `predictions.jsonl` (volume) + Telegram picks
     push (+ email if SMTP configured).
   - Persisting predictions is what makes next-day scoring possible; the UI
     being stateless does not remove this need.

## Cron entry (documented, not auto-installed)

```cron
CRON_TZ=Asia/Jerusalem
0 15 * * * docker exec worldcup-app sh -c 'python main.py --score "$(date -u -d yesterday +%F)" ; python main.py' >> /var/log/worldcup.log 2>&1
```

## Error handling

- Per-match failures in `core.build_items` → `DigestItem` with `note`, never
  abort the page/job.
- Flask: upstream fetch errors → "data unavailable" render, `/healthz` stays up.
- Telegram/SMTP failures → logged, non-fatal.
- Existing `HTTP_TIMEOUT` applies to all outbound calls.

## Testing

- Keep existing offline checks (Poisson sanity, digest render, store roundtrip).
- `core.build_items` with a stubbed `ScheduleSource` + stubbed market client →
  correct items incl. a no-market fixture.
- `send_telegram` with a fake `requests.Session` → correct URL/payload, raises
  when unconfigured.
- Flask: `GET /day/<date>` returns 200 with stubbed `core` (monkeypatch); bad
  date → 400; `/healthz` → 200.
- `docker compose config` validates; container builds.

## Out of scope (YAGNI)

- No DB (jsonl is enough).
- No auth on the dashboard (personal, obscure subdomain; can add later).
- No CLOB live-midpoint refinement (still a future "optional").
- No multi-user / accounts / historical charts beyond per-day accuracy.

## Security / secrets

- Telegram token + SMTP creds live only in `.env` (gitignored) / env.
- Container port bound to `127.0.0.1`; only nginx is public.
- `coda-defense.com` untouched.
```
