"""
News Sentry – Multi‑Ticker High‑Sensitivity News Monitor
=======================================================
Polls **Google News RSS** for multiple companies, deduplicates cross‑source
headlines, classifies relevance with OpenAI, and emails you when a headline
matches any of these triggers:

* Litigation / regulatory action  
* Earnings / financial results  
* Technology developments  
* Collaborations / partnerships  
* **Geo‑political events** (sanctions, conflicts, trade policy, etc.)  
* **Macro‑financial events** (interest‑rate moves, inflation data, central‑bank
  policy, recession signals, debt downgrades, etc.)

Run:
```bash
pip install feedparser apscheduler openai pydantic python-dotenv simhash
python news_sentry.py &> sentry.log &
```

`.env` template:
```
OPENAI_API_KEY=sk-...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=you@example.com
SMTP_PASS=app-password
ALERT_TO=dest@example.com
```
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Dict, List
from urllib.parse import quote_plus

import email.utils as eut
import feedparser
import openai
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from pydantic import BaseModel, HttpUrl
from simhash import Simhash
from zoneinfo import ZoneInfo

# ───────────────────────── Logging ──────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger("news_sentry")

# ─────────────────────── Environment vars ───────────────────
load_dotenv()
for var in (req := ["OPENAI_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_TO"]):
    if not os.getenv(var):
        logger.error("Environment variable %s is missing", var)
        sys.exit(1)
client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ──────────────────────── Configuration ─────────────────────
COMPANIES: List[Dict[str, str]] = [
   # {"ticker": "AMD", "name": "Advanced Micro Devices"},
    {"ticker": "PXLW", "name": "PIXEL Wrks Inc"},
    {"ticker": "QBTS", "name": "D‑Wave Quantum"},
    {"ticker": "IONQ", "name": "IONQ QUANTUM"},
    # Add a special ticker to track general market news.
    {"ticker": "MARKET", "name": "General Market News"},
]


KEYWORDS: Dict[str, List[str]] = {
    "litigation": ["lawsuit", "court", "legal", "litigation", "settlement"],
    "earnings": ["earnings", "results", "quarter", "guidance", "revenue", "profit"],
    "tech": ["technology", "product", "update", "roadmap", "cpu", "gpu", "chip", "launch", "quantum"],
    "collaboration": ["partnership", "collaboration", "alliance", "agreement", "joint venture", "mou"],
    "geo_political": [
        "sanction", "export control", "tariff", "trade war", "regulation", "legislation",
        "government", "policy", "geopolitical", "conflict", "war", "ukraine", "israel",
        "hamas", "china", "iran", "us‑china", "eu",
    ],
    "macro_financial": [
        "interest rate", "rate hike", "rate cut", "federal reserve", "fed", "ecb", "boe",
        "central bank", "inflation", "cpi", "ppi", "gdp", "jobs report", "payrolls",
        "unemployment", "recession", "downgrade", "credit rating", "debt ceiling",
        "stimulus", "quantitative easing", "jerome powell", "powell",
    ],
}

# NEW: Define which keyword categories to use for the "MARKET" ticker.
# Options: "macro_financial", "geo_political". Can be one, both, or empty.
MARKET_NEWS_CATEGORIES: List[str] = ["macro_financial"]

CHECK_INTERVAL_SECONDS = 600  # 10‑minute poll
DB_PATH = "news_sentry.db"
SIMHASH_THRESHOLD = 3

# Dynamically build the list of market-wide keywords from the categories.
MARKET_KEYWORDS: List[str] = [
    kw for category in MARKET_NEWS_CATEGORIES if category in KEYWORDS for kw in KEYWORDS[category]
]

# CORRECTED: Dynamically build RSS URLs. For the special "MARKET" ticker, the
# query will be a broad search based on MARKET_NEWS_CATEGORIES. For
# regular tickers, it will be company-specific.
RSS_URLS = {}
market_query = " OR ".join(f'"{kw}"' for kw in MARKET_KEYWORDS)

for c in COMPANIES:
    if c["ticker"] == "MARKET":
        if not market_query:
            logger.warning("MARKET ticker is configured, but no market keywords were generated. Skipping market news feed.")
            continue
        query = market_query
    else:
        query = f'{c["ticker"]} OR "{c["name"]}"'
    
    RSS_URLS[c["ticker"]] = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )

# ───────────────────────── Data models ──────────────────────
class Article(BaseModel):
    id: str
    published_at: datetime
    headline: str
    summary: str | None = None
    url: HttpUrl
    ticker: str
    simhash: int

# ──────────────────────── SQLite helpers ────────────────────

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # Use a transaction for multiple statements
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS articles (
                          id TEXT PRIMARY KEY,
                          published_at TEXT,
                          ticker TEXT
                       )"""
            )

            # Add simhash column if it doesn't exist
            cursor = conn.execute("PRAGMA table_info(articles)")
            columns = [row[1] for row in cursor.fetchall()]
            if "simhash" not in columns:
                conn.execute("ALTER TABLE articles ADD COLUMN simhash TEXT")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_simhash ON articles(simhash)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pending_alerts (
                          article_id TEXT PRIMARY KEY,
                          article_data TEXT NOT NULL,
                          sentiment INTEGER NOT NULL,
                          ticker TEXT NOT NULL,
                          added_at TEXT NOT NULL,
                          FOREIGN KEY(article_id) REFERENCES articles(id)
                       )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ticker_scores (
                        ticker TEXT PRIMARY KEY,
                        score INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                )"""
            )
    logger.info("SQLite DB ready → %s", DB_PATH)

def already_seen(article_simhash: int, threshold: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.execute("SELECT simhash FROM articles")
        for row in cursor:
            existing_simhash = row[0]
            if existing_simhash and Simhash(article_simhash).distance(Simhash(int(existing_simhash))) <= threshold:
                return True
    return False

def mark_seen(article: Article) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO articles (id, published_at, ticker, simhash) VALUES (?, ?, ?, ?)",
            (article.id, article.published_at.isoformat(), article.ticker, str(article.simhash)),
        )
        conn.commit()

def add_pending_alert(article: Article, sentiment: int) -> None:
    """Stores an alert in the DB to be included in the next daily summary."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO pending_alerts
               (article_id, article_data, sentiment, ticker, added_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                article.id,
                article.json(),
                sentiment,
                article.ticker,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

def get_and_clear_pending_alerts() -> List[tuple[Article, int, str]]:
    """Retrieves all pending alerts and clears the table."""
    alerts = []
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:  # Transaction
            cursor = conn.execute("SELECT article_data, sentiment, ticker FROM pending_alerts ORDER BY added_at")
            rows = cursor.fetchall()
            if not rows:
                return []
            for article_data, sentiment, ticker in rows:
                alerts.append((Article.parse_raw(article_data), sentiment, ticker))
            conn.execute("DELETE FROM pending_alerts")
    logger.info("Retrieved and cleared %d pending alerts.", len(alerts))
    return alerts

def get_ticker_score(ticker: str) -> int | None:
    """Retrieves the current score for a ticker."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT score FROM ticker_scores WHERE ticker = ?", (ticker,)).fetchone()
        return row[0] if row else None

def update_ticker_score(ticker: str, new_sentiment: int) -> int:
    """Updates a ticker's score based on a new sentiment value."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            # Get the last 9 sentiments for this ticker from the database.
            cursor = conn.execute(
                """SELECT T2.sentiment
                    FROM articles AS T1
                    INNER JOIN pending_alerts AS T2 ON T1.id = T2.article_id
                    WHERE T1.ticker = ?
                    ORDER BY T1.published_at DESC
                    LIMIT 9""",
                (ticker,)
            )
            sentiments = [row[0] for row in cursor.fetchall()]
            sentiments.insert(0, new_sentiment)

            # Map sentiment from [-5, 5] to [0, 100].
            scores = [(s + 5) * 10 for s in sentiments]
            avg_score = int(sum(scores) / len(scores))

            conn.execute(
                """INSERT OR REPLACE INTO ticker_scores (ticker, score, updated_at)
                   VALUES (?, ?, ?)""",
                (ticker, avg_score, datetime.utcnow().isoformat()),
            )
    logger.info("Updated score for %s to %d", ticker, avg_score)
    return avg_score

# ─────────────────────── Helper functions ───────────────────

def canonical_headline(head: str) -> str:
    head = re.sub(r"\s*-\s*[^-]+$", "", head).lower()
    return re.sub(r"[^a-z0-9]", "", head)

def quick_filter(text: str) -> bool:
    """
    Performs a stateless keyword check. Returns True if the text is potentially
    relevant, False otherwise.
    - An article is always relevant if it contains market-wide keywords.
    - Otherwise, it's only relevant if it contains other keywords (e.g.,
      'earnings', 'litigation') AND mentions one of the tracked companies.
    """
    text_lower = text.lower()

    # 1. Pass if any market-wide keywords are present.
    if any(kw in text_lower for kw in MARKET_KEYWORDS):
        return True

    # 2. If not, check for company-specific keywords.
    company_kws = {kw for k in ("litigation", "earnings", "tech", "collaboration") for kw in KEYWORDS[k]}
    if not any(kw in text_lower for kw in company_kws):
        return False

    # 3. If company-specific keywords are found, a company MUST be mentioned.
    for company in COMPANIES:
        if company["ticker"] != "MARKET" and (company["ticker"].lower() in text_lower or company["name"].lower() in text_lower):
            return True
    return False


# ───────────────────────── Fetch RSS ────────────────────────

def fetch_news(company: Dict[str, str], since: datetime) -> List[Article]:
    feed = feedparser.parse(RSS_URLS[company["ticker"]])
    arts: List[Article] = []
    for e in feed.entries:
        try:
            pub = eut.parsedate_to_datetime(e.published)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if pub < since:
            continue
        
        full_text = e.title + " " + (e.summary or "")
        
        # ID is based on canonical headline ONLY for global, cross-ticker deduplication.
        canon = canonical_headline(e.title)
        aid = hashlib.sha256(canon.encode()).hexdigest()
        
        arts.append(
            Article(
                id=aid,
                published_at=pub,
                headline=e.title,
                summary=getattr(e, "summary", None),
                url=e.link,
                ticker=company["ticker"],
                simhash=Simhash(full_text).value,
            )
        )
    return arts

# ───────────────────────── OpenAI LLM ───────────────────────

def classify(company: Dict[str, str], article: Article) -> tuple[bool, int]:
    logger.info("  -> Classifying with OpenAI...")
    system = (
        "You are a financial news assistant for US markets. Return JSON with keys 'relevant' (bool) and 'sentiment' (int). "
        "Mark 'relevant' true if the text concerns: (a) litigation, earnings, tech developments or collaborations that directly involve "
        f"{company['name']} ({company['ticker']}), OR (b) geo‑political or macro‑financial events (e.g., central‑bank actions, sanctions, wars) "
        "that are highly likely to affect the US market. Ignore news with limited impact on US markets (e.g., minor international events). "
        "Sentiment scale: −5 very negative, 0 neutral, 5 very positive. Sentiment −5..+5."
    )
    user = f"Headline: {article.headline}\nSummary: {article.summary or ''}"
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        max_tokens=50,
    )
    d = json.loads(resp.choices[0].message.content)
    logger.debug("OpenAI response for %s: %s", article.id, d)
    return bool(d.get("relevant")), int(d.get("sentiment", 0))

# ───────────────────────── Notifications ────────────────────

def send_digest_email(ticker: str, alerts: List[tuple[Article, int]], score: int | None) -> None:
    """Formats and sends a single email with multiple news alerts for a ticker."""
    score_display = f"{score}" if score is not None else "N/A"
    msg = EmailMessage()
    msg["Subject"] = f"{ticker} News Digest ({len(alerts)} articles, Score: {score_display})"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_TO"]

    # Sort by sentiment, most impactful first
    sorted_alerts = sorted(alerts, key=lambda x: abs(x[1]), reverse=True)

    article_summaries = []
    for art, sent in sorted_alerts:
        part = "\n".join(
            [f"Headline: {art.headline}", f"Sentiment: {sent:+}", f"URL: {art.url}"]
        )
        article_summaries.append(part)

    title = f"Found {len(alerts)} relevant news articles for {ticker} (Score: {score_display}):"
    full_body = title + "\n\n" + "\n\n---\n\n".join(article_summaries)
    msg.set_content(full_body)

    ctx = smtplib.ssl.create_default_context()
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), context=ctx) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    logger.info("🔔 [%s] Digest email sent with %d articles.", ticker, len(alerts))

# ───────────────────────── Scheduler job ───────────────────

def send_daily_digest() -> None:
    """
    Scheduled to run once daily. Fetches all queued alerts from the DB,
    groups them by ticker, sends a summary email for each, and clears the queue.
    """
    logger.info("Running daily digest job...")
    pending_alerts = get_and_clear_pending_alerts()

    if not pending_alerts:
        logger.info("No pending alerts to send.")
        return

    # Group alerts by ticker
    alerts_by_ticker: Dict[str, List[tuple[Article, int]]] = {}
    for art, sent, ticker in pending_alerts:
        if ticker not in alerts_by_ticker:
            alerts_by_ticker[ticker] = []
        alerts_by_ticker[ticker].append((art, sent))

    for ticker, alerts in alerts_by_ticker.items():
        try:
            score = get_ticker_score(ticker)
            send_digest_email(ticker, alerts, score)
        except Exception as e:
            logger.error("Failed to send digest email for %s: %s", ticker, e, exc_info=True)

def job() -> None:
    since = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=6)
    logger.info("Starting news processing cycle...")

    # 1. Fetch all articles from all feeds into a single list.
    all_articles: List[Article] = []
    for company in COMPANIES:
        try:
            fetched = fetch_news(company, since)
            all_articles.extend(fetched)
        except Exception as e:
            logger.error("Failed to fetch news for %s: %s", company["ticker"], e)

    # 2. Group articles by unique ID to process each story only once.
    unique_articles: Dict[str, Article] = {art.id: art for art in all_articles}
    logger.info("Fetched %d articles (%d unique) across all feeds.", len(all_articles), len(unique_articles))

    company_map = {c['ticker']: c for c in COMPANIES}
    
    for article_id, article in unique_articles.items():
        if already_seen(article.simhash, SIMHASH_THRESHOLD):
            continue

        logger.info("📰 Processing [%s]: %s", article.ticker, article.headline)

        # 3. Quick filter (now stateless and more robust).
        full_text = article.headline + " " + (article.summary or "")
        if not quick_filter(full_text):
            logger.info("  -> Skipping (failed quick_filter).")
            mark_seen(article)
            continue

        # 4. Classify and Alert.
        company_context = company_map[article.ticker]
        relevant, sentiment = classify(company_context, article)
        if relevant:
            logger.info("  -> Queuing alert for daily digest.")
            add_pending_alert(article, sentiment)
            if article.ticker != "MARKET":
                update_ticker_score(article.ticker, sentiment)
        
        mark_seen(article)

    logger.info("Cycle complete.")

# ───────────────────────── Main entry ───────────────────────
if __name__ == "__main__":
    init_db()
    sched = BackgroundScheduler(timezone=timezone.utc)
    # Job to poll for new articles every 10 minutes
    sched.add_job(job, IntervalTrigger(seconds=CHECK_INTERVAL_SECONDS), next_run_time=datetime.utcnow())
    # Job to send the daily digest email one hour before US market open (08:30 New York time)
    market_tz = ZoneInfo("America/New_York")
    digest_job = sched.add_job(send_daily_digest, CronTrigger(hour=8, minute=30, timezone=market_tz))
    sched.start()
    logger.info("Scheduler running for %d tickers – %ds interval (Ctrl‑C to stop)", len(COMPANIES), CHECK_INTERVAL_SECONDS)
    israel_tz = ZoneInfo("Asia/Jerusalem")
    if digest_job.next_run_time:
        logger.info(
            "Daily news digest scheduled for %s Israel time.",
            digest_job.next_run_time.astimezone(israel_tz).strftime("%Y-%m-%d %H:%M"),
        )
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        logger.info("Shutdown complete")
