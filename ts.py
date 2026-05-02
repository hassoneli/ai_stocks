from __future__ import annotations

#!/usr/bin/env python3
"""
AI Stock Ticker Search (ts.py)
-------------------------------------------------
Searches for opportunities in the NYSE and sends a daily email with the findings.
An opportunity is defined as a stock that has experienced a significant price drop
and has strong fundamentals (low P/E ratio and high EPS growth).

Required **environment variables** (script aborts if any are missing):
    ALERTS_EMAIL_FROM       Sender address
    ALERTS_EMAIL_TO         Recipient address
    ALERTS_SMTP_SERVER      SMTP host
    ALERTS_SMTP_PORT        SMTP SSL port (465)
    ALERTS_SMTP_USER        SMTP username
    ALERTS_SMTP_PASS        SMTP password / app‑password
    OPENAI_API_KEY          OpenAI API Key
"""

import argparse
import datetime as dt
import logging
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from urllib.error import HTTPError

import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------# Logging# ---------------------------------------------------------------------------#
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------# Environment & configuration# ---------------------------------------------------------------------------#
load_dotenv()
REQUIRED_VARS = [
    "ALERTS_EMAIL_FROM",
    "ALERTS_EMAIL_TO",
    "ALERTS_SMTP_SERVER",
    "ALERTS_SMTP_PORT",
    "ALERTS_SMTP_USER",
    "ALERTS_SMTP_PASS",
    "OPENAI_API_KEY",
]
missing = [v for v in REQUIRED_VARS if v not in os.environ]
if missing:
    logger.error("Missing required environment variables: %s", ", ".join(missing))
    sys.exit(1)

EMAIL_FROM: str = os.environ["ALERTS_EMAIL_FROM"]
EMAIL_TO: str = os.environ["ALERTS_EMAIL_TO"]
SMTP_SERVER: str = os.environ["ALERTS_SMTP_SERVER"]
SMTP_PORT: int = int(os.environ["ALERTS_SMTP_PORT"])
SMTP_USER: str = os.environ["ALERTS_SMTP_USER"]
SMTP_PASS: str = os.environ["ALERTS_SMTP_PASS"]
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

with open('nyse_tech_companies_with_desc.txt', 'r', encoding='utf-8') as f:
    TICKERS = [line.split(';')[0] for line in f if line.strip()]

# Opportunity thresholds
# These are just examples, they can be fine-tuned.
MAX_PE_RATIO = 25
MIN_EPS_GROWTH = 0.10 # 10%
MIN_PRICE_DROP = -0.05 # 5% drop
QUANTUM_TICKERS = [
    'IONQ', 'RGTI', 'QBTS', 'QUBT', 'ATOM', 'QNCCF', 'ZPTA'
]

# ---------------------------------------------------------------------------# Email helper# ---------------------------------------------------------------------------#
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

# ---------------------------------------------------------------------------# Core logic# ---------------------------------------------------------------------------#

def get_stock_info(ticker: str) -> dict | None:
    """Fetches key financial data for a given stock ticker."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        # A robust check for invalid ticker
        if not info or info.get('regularMarketPrice') is None and info.get('currentPrice') is None:
             logger.warning("No data found for ticker %s, it may be delisted.", ticker)
             return None

        hist = stock.history(period="2d")
        if len(hist) < 2:
            logger.warning("Not enough historical data for ticker %s to calculate price change.", ticker)
            return None
        prev_close = hist['Close'].iloc[0]
        price = info.get("currentPrice") or info.get("previousClose")
        price_change = (price - prev_close) / prev_close if prev_close else 0

        return {
            "ticker": ticker,
            "longName": info.get("longName"),
            "pe_ratio": info.get("trailingPE"),
            "forward_eps": info.get("forwardEps"),
            "trailing_eps": info.get("trailingEps"),
            "price": price,
            "price_change": price_change,
        }
    except HTTPError as e:
        if e.code == 404:
            logger.warning("HTTP 404 Not Found for ticker %s. It may be delisted.", ticker)
        else:
            logger.exception("HTTP error for ticker %s", ticker)
        return None
    except Exception:
        logger.exception("Could not get info for ticker %s", ticker)
        return None

def get_openai_analysis(ticker, company_name, price, pe_ratio, eps_growth, price_change):
    """Gets analysis from OpenAI."""
    pe_ratio_str = f"{pe_ratio:.2f}" if pe_ratio is not None else "N/A"
    eps_growth_str = f"{eps_growth*100:.2f}%" if eps_growth is not None else "N/A"
    price_change_str = f"{price_change*100:.2f}%" if price_change is not None else "N/A"
    prompt = f"Provide a brief analysis for investing in {company_name} ({ticker}). The current price is ${price:.2f}, which has changed by {price_change_str} in the last day. The P/E ratio is {pe_ratio_str}, and EPS growth is {eps_growth_str}. Is this a good investment opportunity and why? Provide a short summary."
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a financial analyst providing investment advice."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            n=1,
            stop=None,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("Could not get OpenAI analysis for %s", ticker)
        return "N/A"

def get_quantum_openai_analysis(ticker, company_name, price):
    """Gets analysis from OpenAI for quantum companies."""
    prompt = f"Provide a brief analysis for investing in {company_name} ({ticker}). The current price is ${price:.2f}. This is a quantum computing company, so traditional financial metrics may not be relevant. Instead, focus on the company's technology, recent news, partnerships, and overall potential in the quantum computing industry. Is this a good investment opportunity and why? Provide a short summary."
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a financial analyst providing investment advice for emerging technologies."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            n=1,
            stop=None,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("Could not get OpenAI analysis for %s", ticker)
        return "N/A"

def find_opportunities():
    """
    Finds and prints stock opportunities based on price drop and fundamentals.
    """
    logger.info("Searching for opportunities in NYSE...")
    opportunities = []
    potential_opportunities = []
    quantum_opportunities = []

    for ticker in TICKERS:
        info = get_stock_info(ticker)
        if not info:
            continue

        price_change = info.get("price_change")
        has_price_dropped = price_change is not None and price_change < MIN_PRICE_DROP

        if not has_price_dropped:
            continue

        price_val = info.get("price")
        price = None
        if price_val is not None:
            try:
                price = float(price_val)
            except (ValueError, TypeError):
                logger.warning("Could not convert price '%s' to float for ticker %s", price_val, ticker)

        company_name = info.get("longName", ticker)

        if price is None:
            logger.info("Skipping %s due to missing price data.", ticker)
            continue

        if ticker in QUANTUM_TICKERS:
            analysis = get_quantum_openai_analysis(ticker, company_name, price)
            quantum_opportunities.append({
                "ticker": ticker,
                "company_name": company_name,
                "price": price,
                "price_change": price_change,
                "analysis": analysis,
            })
            logger.info("Found quantum opportunity: %s", ticker)
            continue

        pe_ratio_val = info.get("pe_ratio")
        pe_ratio = None
        if pe_ratio_val is not None:
            try:
                pe_ratio = float(pe_ratio_val)
            except (ValueError, TypeError):
                logger.warning("Could not convert P/E ratio '%s' to float for ticker %s", pe_ratio_val, ticker)

        forward_eps_val = info.get("forward_eps")
        forward_eps = None
        if forward_eps_val is not None:
            try:
                forward_eps = float(forward_eps_val)
            except (ValueError, TypeError):
                logger.warning("Could not convert forward EPS '%s' to float for ticker %s", forward_eps_val, ticker)

        trailing_eps_val = info.get("trailing_eps")
        trailing_eps = None
        if trailing_eps_val is not None:
            try:
                trailing_eps = float(trailing_eps_val)
            except (ValueError, TypeError):
                logger.warning("Could not convert trailing EPS '%s' to float for ticker %s", trailing_eps_val, ticker)

        has_low_pe = pe_ratio is not None and pe_ratio < MAX_PE_RATIO
        
        eps_growth = None
        if forward_eps is not None and trailing_eps is not None and trailing_eps > 0:
            eps_growth = (forward_eps - trailing_eps) / trailing_eps
        
        has_high_eps_growth = eps_growth is not None and eps_growth > MIN_EPS_GROWTH

        if has_low_pe and has_high_eps_growth:
            opportunity = {
                "ticker": ticker,
                "company_name": company_name,
                "price": price,
                "price_change": price_change,
                "pe_ratio": pe_ratio,
                "eps_growth": eps_growth,
            }
            opportunities.append(opportunity)
            logger.info(
                "Found opportunity: %s (Price: $%.2f, Change: %.2f%%, P/E: %.2f, EPS Growth: %.2f%%)",
                ticker, price, price_change * 100, pe_ratio, eps_growth * 100
            )
        elif has_low_pe or has_high_eps_growth:
            reason = []
            if has_low_pe:
                reason.append("Low P/E")
            if has_high_eps_growth:
                reason.append("High EPS Growth")
            
            potential_opp = {
                "ticker": ticker,
                "price": price,
                "price_change": price_change,
                "pe_ratio": pe_ratio,
                "eps_growth": eps_growth,
                "reason": " and ".join(reason)
            }
            potential_opportunities.append(potential_opp)
            logger.info(
                "Found potential opportunity: %s (%s)",
                ticker, potential_opp["reason"]
            )

    body = ""
    subject = "Stock Opportunities"

    if opportunities:
        # Sort by price drop desc
        opportunities.sort(key=lambda x: x["price_change"])
        
        # Get top 10 for analysis
        top_10_opportunities = opportunities[:10]
        
        analyzed_opportunities = []
        for op in top_10_opportunities:
            analysis = get_openai_analysis(op["ticker"], op["company_name"], op["price"], op["pe_ratio"], op["eps_growth"], op["price_change"])
            op_with_analysis = op.copy()
            op_with_analysis["analysis"] = analysis
            analyzed_opportunities.append(op_with_analysis)

        # Get top 3 for email
        top_3_opportunities = analyzed_opportunities[:3]

        subject = "Top 3 Stock Opportunities of the Day"
        body += "Here are the top 3 stock opportunities found today based on price drop and fundamentals:\n\n"
        for op in top_3_opportunities:
            body += f"- {op['ticker']} ({op['company_name']}): Price=${op['price']:.2f}, Change={op['price_change']*100:.2f}%, P/E={op['pe_ratio']:.2f}, EPS Growth={op['eps_growth']*100:.2f}%\n"
            body += f"  AI Analysis: {op['analysis']}\n\n"
    
    if quantum_opportunities:
        body += "\n--- Quantum Computing Analysis ---\n\n"
        for op in quantum_opportunities:
            body += f"- {op['ticker']} ({op['company_name']}): Price=${op['price']:.2f}, Change={op['price_change']*100:.2f}%\n"
            body += f"  AI Analysis: {op['analysis']}\n\n"

    if potential_opportunities:
        body += "\nHere are some potential opportunities to look into:\n\n"
        for op in potential_opportunities:
            price_change_str = f"Change={op['price_change']*100:.2f}%" if op['price_change'] is not None else "Change=N/A"
            pe_str = f"P/E={op['pe_ratio']:.2f}" if op['pe_ratio'] is not None else "P/E=N/A"
            eps_growth_str = f"EPS Growth={op['eps_growth']*100:.2f}%" if op['eps_growth'] is not None else "EPS Growth=N/A"
            body += f"- {op['ticker']}: Price=${op['price']:.2f}, {price_change_str}, {pe_str}, {eps_growth_str} (Reason: {op['reason']})\n"

    if body:
        send_email(subject, body)
    else:
        logger.info("No opportunities found today.")
        send_email("No Stock Opportunities Today", "No stock opportunities were found based on the current criteria.")





def main():
    """Main function."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="Run the opportunity search immediately.")
    args = parser.parse_args()

    if args.now:
        find_opportunities()
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    # Job to find opportunities every day at 8 AM UTC
    sched.add_job(find_opportunities, "cron", hour=8)
    sched.start()

    logger.info("Ticker search started. Will run daily at 8 AM UTC.")
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler...")
        sched.shutdown()

if __name__ == "__main__":
    main()
