#!/usr/bin/env python3
"""
AI Stock Price Monitor (env‑strict, intraday‑aware)
-------------------------------------------------
Watches selected tickers and sends a single e‑mail alert when the price moves
±N % from **previous close** *during regular U.S. market hours*.

Why this revision?
    • Earlier versions treated an empty 1‑minute history before 09:30 ET as a
      “delisted” error. In reality, the market simply wasn’t open yet.  
    • We now detect U.S. market session times and skip polling outside them,
      preventing bogus warnings for tickers like AMD.

Required **environment variables** (script aborts if any are missing):
    ALERTS_EMAIL_FROM       Sender address
    ALERTS_EMAIL_TO         Recipient address
    ALERTS_SMTP_SERVER      SMTP host
    ALERTS_SMTP_PORT        SMTP SSL port (465)
    ALERTS_SMTP_USER        SMTP username
    ALERTS_SMTP_PASS        SMTP password / app‑password

Optional variables (defaults in parentheses):
    ALERTS_DB_PATH          SQLite DB path (alerts.db)
    ALERTS_TICKERS          CSV tickers to monitor (AAPL,MSFT,GOOGL,TSLA)
    ALERTS_THRESHOLD        Percent move that triggers alert (4)
    ALERTS_INTERVAL_SEC     Polling interval in seconds (300)

Install requirements:
    pip install yfinance apscheduler python-dotenv

Run:
    python price_monitor.py
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
import sys
import time
import pytz
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment & configuration
# ---------------------------------------------------------------------------
load_dotenv()
REQUIRED_VARS = [
    "ALERTS_EMAIL_FROM",
    "ALERTS_EMAIL_TO",
    "ALERTS_SMTP_SERVER",
    "ALERTS_SMTP_PORT",
    "ALERTS_SMTP_USER",
    "ALERTS_SMTP_PASS",
]
missing = [v for v in REQUIRED_VARS if v not in os.environ]
if missing:
    logger.error("Missing required environment variables: %s", ", ".join(missing))
    sys.exit(1)

DB_PATH: str = os.environ.get("ALERTS_DB_PATH", "alerts.db")
TICKERS: list[str] = [t.strip().upper() for t in os.environ.get("ALERTS_TICKERS", "AAPL,MSFT,GOOGL,TSLA").split(",") if t.strip()]
THRESHOLD: float = float(os.environ.get("ALERTS_THRESHOLD", "4"))
INTERVAL_SEC: int = int(os.environ.get("ALERTS_INTERVAL_SEC", "300"))

EMAIL_FROM: str = os.environ["ALERTS_EMAIL_FROM"]
EMAIL_TO: str = os.environ["ALERTS_EMAIL_TO"]
SMTP_SERVER: str = os.environ["ALERTS_SMTP_SERVER"]
SMTP_PORT: int = int(os.environ["ALERTS_SMTP_PORT"])
SMTP_USER: str = os.environ["ALERTS_SMTP_USER"]
SMTP_PASS: str = os.environ["ALERTS_SMTP_PASS"]

ET_TZ = ZoneInfo("US/Eastern")
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(16, 0)

# In-memory tracker for price alerts sent today. Reset daily by a scheduled job.
ALERTS_SENT_TODAY: set[str] = set()

# Tracker for tickers that have exceeded threshold and need hourly alerts
THRESHOLD_EXCEEDED_TICKERS: dict[str, dict] = {}

def reset_daily_alerts() -> None:
    """
    Scheduled to run once a day to clear the set of tickers that have had
    price alerts sent for the current day.
    """
    global ALERTS_SENT_TODAY
    ALERTS_SENT_TODAY.clear()
    logger.info("Daily price alert tracker has been reset.")

# ---------------------------------------------------------------------------
# Price fetch & mail helpers
# ---------------------------------------------------------------------------

def market_session_now() -> str:
    """Return 'pre', 'open', or 'post' based on U.S. Eastern time."""
    now_et = dt.datetime.now(ET_TZ).time()
    if now_et < MARKET_OPEN:
        return "pre"
    if now_et > MARKET_CLOSE:
        return "post"
    return "open"


NY_TZ = pytz.timezone("America/New_York")

def fetch_price_and_change(raw_ticker: str, force: bool = False) -> tuple[float, float] | None:
    """
    Return (pct_change_since_prev_close, last_price) or None
    when the US equity market is closed / no data yet.
    """
    ticker = raw_ticker.lstrip("$")
    now_ny = dt.datetime.now(tz=NY_TZ)

    # — 1 — session-open guard ------------------------------------------------
    if not force and (
        now_ny.weekday() >= 5                # weekend
        or now_ny.time() < dt.time(9, 30)    # pre-market
        or now_ny.time() >= dt.time(16, 0)   # post-close
    ):
        logger.info("%s: market closed – skipping", ticker)
        return None

    today = now_ny.date()

    # — 2 — authoritative previous close ---------------------------------------
    daily = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
    if daily.empty:
        logger.info("%s: no daily data", ticker)
        return None

    past_daily = daily[daily.index.date < today]
    if past_daily.empty:
        logger.info("%s: no past daily data", ticker)
        return None

    prev_close = float(past_daily["Close"].iloc[-1])  # previous close
    logger.info("prev_close: %.2f", prev_close)

    # — 3 — latest trade price (1-min bar) -----------------------------------
    minute = yf.Ticker(ticker).history(
        start=today.isoformat(),
        end=(today + dt.timedelta(days=1)).isoformat(),
        interval="1m",
        auto_adjust=False,
    )
    if minute.empty or minute["Close"].dropna().empty:
        logger.info("%s: no minute data yet", ticker)
        return None

    last_price = float(minute["Close"].dropna().iloc[-1])
    pct_change = (last_price - prev_close) / prev_close * 100
    logger.info("pct_change: %.2f last price: %.2f", pct_change, last_price)

    return pct_change, last_price



def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        logger.info("📧 Sent e‑mail: %s", subject)
    except smtplib.SMTPException:
        logger.exception("SMTP failure sending alert")
    except Exception:
        logger.exception("Unexpected error sending email")


# ---------------------------------------------------------------------------
# Core monitoring loop
# ---------------------------------------------------------------------------

def check_ticker(ticker: str, force: bool = False) -> tuple[float, float] | None:
    result = fetch_price_and_change(ticker, force=force)
    if result is None:
        return None

    pct, last_price = result
    logger.info("%s: price=$%.2f, change=%.2f%%", ticker, last_price, pct)

    # Track threshold exceeded tickers
    if abs(pct) >= THRESHOLD:
        if ticker not in THRESHOLD_EXCEEDED_TICKERS:
            THRESHOLD_EXCEEDED_TICKERS[ticker] = {
                'pct_change': pct,
                'last_price': last_price,
                'alert_sent': False,
                'first_exceeded': dt.datetime.now(ET_TZ)
            }
        else:
            THRESHOLD_EXCEEDED_TICKERS[ticker]['pct_change'] = pct
            THRESHOLD_EXCEEDED_TICKERS[ticker]['last_price'] = last_price
            
        # Send initial alert if not already sent
        if ticker not in ALERTS_SENT_TODAY:
            direction = "up" if pct > 0 else "down"
            subject = f"{ticker} moved {pct:.2f}% {direction} today"
            body = (
                f"{ticker} has moved {pct:.2f}% {direction} from previous close, "
                f"exceeding the {THRESHOLD}% threshold."
            )
            send_email(subject, body)
            ALERTS_SENT_TODAY.add(ticker)
            logger.info(body)
    else:
        # Reset tracking if price is back within threshold
        if ticker in THRESHOLD_EXCEEDED_TICKERS:
            del THRESHOLD_EXCEEDED_TICKERS[ticker]

    return result


def send_hourly_alerts() -> None:
    """Send hourly email alerts for tickers that have exceeded the threshold."""
    now = dt.datetime.now(ET_TZ)
    
    for ticker, data in list(THRESHOLD_EXCEEDED_TICKERS.items()):
        # Only send hourly alerts if the threshold was exceeded and alert hasn't been sent yet
        if not data['alert_sent']:
            # Check if it's been at least an hour since the threshold was first exceeded
            time_diff = now - data['first_exceeded']
            if time_diff.total_seconds() >= 3600:  # 1 hour
                pct = data['pct_change']
                direction = "up" if pct > 0 else "down"
                subject = f"{ticker} still {pct:.2f}% {direction} (hourly update)"
                body = (
                    f"{ticker} has remained {pct:.2f}% {direction} from previous close "
                    f"for over an hour. Threshold: {THRESHOLD}%."
                )
                send_email(subject, body)
                data['alert_sent'] = True
                logger.info(body)


def send_end_of_day_update() -> None:
    """
    Fetches the final prices at the end of the trade day and sends
    a single combined email update for all monitored tickers.
    Also ensures any final threshold alerts are processed.
    """
    logger.info("Running end of trade-day price checks")
    lines = []
    for ticker in TICKERS:
        try:
            result = check_ticker(ticker, force=True)
            if result is not None:
                pct, last_price = result
                lines.append(f"• {ticker}: ${last_price:.2f} ({pct:+.2f}%)")
            else:
                lines.append(f"• {ticker}: No data available")
        except Exception:
            logger.exception("Error during final trade-day check for %s", ticker)
            lines.append(f"• {ticker}: Error fetching price")

    if lines:
        subject = "End of Trade-Day Price Update"
        body = (
            "Here is the final price update for your monitored tickers at the market close:\n\n"
            + "\n".join(lines)
        )
        send_email(subject, body)
        logger.info("Sent end of trade-day price update email.")


def poll_once() -> None:
    logger.info("Starting polling cycle")
    for t in TICKERS:
        try:
            check_ticker(t)
        except Exception:
            logger.exception("Unhandled error processing %s", t)
    logger.info("Completed polling cycle")


def main() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    # Job to poll for price changes every N seconds
    sched.add_job(poll_once, "interval", seconds=INTERVAL_SEC, next_run_time=dt.datetime.now(dt.timezone.utc))
    # Job to reset the daily alert tracker
    sched.add_job(reset_daily_alerts, "cron", hour=0, minute=15)  # Reset daily at 00:15 UTC
    # Job to send hourly alerts for tickers that have exceeded threshold
    sched.add_job(send_hourly_alerts, "interval", minutes=60)
    # Job to send the final price update at the end of the trade-day (16:00 US/Eastern)
    sched.add_job(
        send_end_of_day_update,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=0,
        timezone=ET_TZ,
    )
    sched.start()

    logger.info(
        "Monitoring %s for ±%.1f%% moves every %s seconds.",
        ", ".join(TICKERS), THRESHOLD, INTERVAL_SEC,
    )
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler...")
        sched.shutdown()


if __name__ == "__main__":
    main()
