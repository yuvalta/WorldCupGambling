"""Central configuration. Everything env-driven; secrets come from env only.

No secret literals live here. SMTP credentials are read from the environment
(and, in hosted runs, from GitHub Actions repo Secrets) — never committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# --- Schedule source -------------------------------------------------------
# openfootball publishes free JSON for the World Cup. The 2026 feed path can
# move around as the project finalises it, so it stays overridable via env.
# Schema (verified for prior cups):
#   {"name": ..., "matches": [{date, time:"13:00 UTC-6", team1, team2,
#                              group, round, ground, score1?, score2?}]}
OPENFOOTBALL_URL = _env(
    "OPENFOOTBALL_URL",
    "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
)

# --- Polymarket Gamma API --------------------------------------------------
GAMMA_BASE = _env("GAMMA_BASE", "https://gamma-api.polymarket.com")
# Optional CLOB base for the (low-priority) live-midpoint refinement step.
CLOB_BASE = _env("CLOB_BASE", "https://clob.polymarket.com")

# --- Poisson model ---------------------------------------------------------
# Max goals per side considered when summing the Poisson grid. 10 is plenty
# for football; keeps the hand-rolled sums cheap.
MAX_GOALS = _env_int("MAX_GOALS", 10)
# Grid search resolution for fitting (lambda1, lambda2) to market odds.
LAMBDA_MAX = float(_env("LAMBDA_MAX", "5.0"))
LAMBDA_STEP = float(_env("LAMBDA_STEP", "0.05"))

# --- HTTP ------------------------------------------------------------------
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 20)
USER_AGENT = _env("USER_AGENT", "wc-gambling-emailer/1.0 (+stdlib+requests)")

# --- Storage ---------------------------------------------------------------
# Defaults under data/ so a Docker volume can persist it across container runs.
PREDICTIONS_PATH = _env("PREDICTIONS_PATH", "data/predictions.jsonl")

# --- Web app ---------------------------------------------------------------
WEB_HOST = _env("WEB_HOST", "0.0.0.0")
WEB_PORT = _env_int("WEB_PORT", 8000)


@dataclass(frozen=True)
class EmailConfig:
    """SMTP settings, all from env. `configured` gates real sends."""

    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.recipient)


def load_email_config() -> EmailConfig:
    return EmailConfig(
        host=_env("SMTP_HOST"),
        port=_env_int("SMTP_PORT", 465),
        user=_env("SMTP_USER"),
        password=_env("SMTP_PASSWORD"),
        sender=_env("EMAIL_FROM") or _env("SMTP_USER"),
        recipient=_env("EMAIL_TO"),
    )


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram Bot API settings, from env. `configured` gates real sends."""

    token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


def load_telegram_config() -> TelegramConfig:
    return TelegramConfig(
        token=_env("TELEGRAM_BOT_TOKEN"),
        chat_id=_env("TELEGRAM_CHAT_ID"),
    )
