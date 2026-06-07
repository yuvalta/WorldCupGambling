"""Digest rendering + email delivery.

`render_digest` returns (subject, text_body, html_body). The copy keeps the
honesty rule front and centre: probabilities are the market's, the scoreline
is our Poisson model's. `send_email` does SMTP-over-SSL using env creds.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

import requests

import config
from config import EmailConfig, TelegramConfig
from predict import Prediction
from schedule_source import Match

DISCLAIMER = (
    "Probabilities are Polymarket's crowd-implied odds (real market data). "
    "The scoreline is our own independent-Poisson model fitted to those odds "
    "— not a Polymarket prediction."
)


@dataclass
class DigestItem:
    """One fixture's line in the digest. `prediction` is None if no market."""

    match: Match
    prediction: Optional[Prediction]
    note: str = ""  # e.g. "no live market found"


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _item_text(item: DigestItem) -> str:
    m = item.match
    head = f"{m.team1} vs {m.team2}"
    if m.group:
        head += f"  (Group {m.group})"
    if item.prediction is None:
        return f"{head}\n    {item.note or 'no live market found'}"
    p = item.prediction
    tw = p.three_way
    lines = [
        head,
        f"    Market odds: {m.team1} {_pct(tw.p1)} | Draw {_pct(tw.draw)} | {m.team2} {_pct(tw.p2)}",
        f"    Model scoreline: {m.team1} {p.scoreline[0]}–{p.scoreline[1]} {m.team2}"
        f"  (xG {p.lambda1:.2f}–{p.lambda2:.2f})",
    ]
    if p.totals is not None:
        lines.append(
            f"    Goals O/U {p.totals.line}: over {_pct(p.totals.p_over)} / under {_pct(p.totals.p_under)}"
        )
    return "\n".join(lines)


def _item_html(item: DigestItem) -> str:
    m = item.match
    head = f"{m.team1} vs {m.team2}"
    grp = f' <span style="color:#888">(Group {m.group})</span>' if m.group else ""
    if item.prediction is None:
        return (
            f'<div style="margin:0 0 16px"><strong>{head}</strong>{grp}'
            f'<div style="color:#a00">{item.note or "no live market found"}</div></div>'
        )
    p = item.prediction
    tw = p.three_way
    totals = ""
    if p.totals is not None:
        totals = (
            f'<div style="color:#555">Goals O/U {p.totals.line}: '
            f"over {_pct(p.totals.p_over)} / under {_pct(p.totals.p_under)}</div>"
        )
    return (
        f'<div style="margin:0 0 16px">'
        f"<strong>{head}</strong>{grp}"
        f'<div style="color:#333">Market odds: {m.team1} <b>{_pct(tw.p1)}</b> · '
        f"Draw <b>{_pct(tw.draw)}</b> · {m.team2} <b>{_pct(tw.p2)}</b></div>"
        f'<div style="color:#0a6">Model scoreline: {m.team1} '
        f"<b>{p.scoreline[0]}–{p.scoreline[1]}</b> {m.team2} "
        f'<span style="color:#888">(xG {p.lambda1:.2f}–{p.lambda2:.2f})</span></div>'
        f"{totals}"
        f"</div>"
    )


def render_digest(date: str, items: List[DigestItem]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for the day's digest."""
    subject = f"World Cup picks — {date} ({len(items)} match{'es' if len(items) != 1 else ''})"

    if not items:
        text = f"No World Cup matches scheduled for {date}."
        html = f"<p>No World Cup matches scheduled for {date}.</p>"
        return subject, text, html

    text_body = "\n\n".join(
        [f"World Cup picks for {date}", "-" * 40]
        + [_item_text(i) for i in items]
        + ["-" * 40, DISCLAIMER]
    )

    html_items = "".join(_item_html(i) for i in items)
    html_body = (
        f'<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:640px">'
        f'<h2 style="margin:0 0 4px">World Cup picks — {date}</h2>'
        f"{html_items}"
        f'<hr style="border:none;border-top:1px solid #ddd;margin:16px 0">'
        f'<p style="color:#888;font-size:12px">{DISCLAIMER}</p>'
        f"</div>"
    )
    return subject, text_body, html_body


def send_email(cfg: EmailConfig, subject: str, text_body: str, html_body: str) -> None:
    """Send a multipart text+HTML email over SMTP/SSL.

    Raises if `cfg` is not fully configured — callers should gate on
    `cfg.configured` (or use --dry-run) to avoid this.
    """
    if not cfg.configured:
        raise RuntimeError("Email not configured: set SMTP_* and EMAIL_TO env vars")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = cfg.recipient
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=30) as server:
        server.login(cfg.user, cfg.password)
        server.sendmail(cfg.sender, [cfg.recipient], msg.as_string())


def send_telegram(cfg: TelegramConfig, text: str) -> None:
    """Push a plain-text message via the Telegram Bot API.

    Plain text only (no parse_mode) so team names with special characters can't
    break Markdown/HTML parsing. Raises if not configured — gate on
    `cfg.configured`.
    """
    if not cfg.configured:
        raise RuntimeError("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{cfg.token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": cfg.chat_id, "text": text, "disable_web_page_preview": True},
        timeout=config.HTTP_TIMEOUT,
        headers={"User-Agent": config.USER_AGENT},
    )
    resp.raise_for_status()
