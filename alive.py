#!/usr/bin/env python3
"""



SH Server Aliveness Monitor
---------------------------
Connects to SSH servers defined in `.servers_env` hourly (in daemon mode
or via crontab), logs status history to SQLite, sends alert emails
when servers are down, and sends a weekly status report email.

Required environment variables in `.env` (for email alerts):
    ALERTS_EMAIL_FROM       Sender address
    ALERTS_EMAIL_TO         Recipient address
    ALERTS_SMTP_SERVER      SMTP host
    ALERTS_SMTP_PORT        SMTP SSL port (465)
    ALERTS_SMTP_USER        SMTP username
    ALERTS_SMTP_PASS        SMTP password / app‑password

Optional environment variables:
    ALIVE_DB_PATH           SQLite database path (default: alive_history.db)
    ALIVE_WEEKLY_DAY        Day for weekly report in daemon mode (default: mon)
    ALIVE_WEEKLY_HOUR       Hour for weekly report in daemon mode (default: 9)

SSH servers are configured in `.servers_env` with patterns like:
    SERVER_PROD_HOST=192.168.1.10
    SERVER_PROD_PORT=22
    SERVER_PROD_USER=ubuntu
    SERVER_PROD_KEY_PATH=~/.ssh/id_rsa
    
Usage:
    # Run check once immediately (useful for hourly cron jobs)
    python alive.py
    
    # Send weekly status report email immediately
    python alive.py --weekly-report
    
    # Run as a daemon that checks every hour and sends weekly email every Monday
    python alive.py --daemon
"""

import argparse
import datetime as dt
import logging
import os
import socket
import smtplib
import sqlite3
import sys
import time
from email.message import EmailMessage

import paramiko
from dotenv import load_dotenv, dotenv_values

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Load main .env for SMTP credentials
load_dotenv()

# SMTP Configuration parsing (supports standard and ALERTS_ prefix)
EMAIL_FROM = os.environ.get("ALERTS_EMAIL_FROM") or os.environ.get("SMTP_USER") or os.environ.get("ALERTS_SMTP_USER")
EMAIL_TO = os.environ.get("ALERTS_EMAIL_TO") or os.environ.get("ALERT_TO")
SMTP_SERVER = os.environ.get("ALERTS_SMTP_SERVER") or os.environ.get("SMTP_HOST")
SMTP_PORT_RAW = os.environ.get("ALERTS_SMTP_PORT") or os.environ.get("SMTP_PORT")
SMTP_PORT = int(SMTP_PORT_RAW) if SMTP_PORT_RAW else 465
SMTP_USER = os.environ.get("ALERTS_SMTP_USER") or os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("ALERTS_SMTP_PASS") or os.environ.get("SMTP_PASS")

VERSION = "1.1.0"

DB_PATH = os.environ.get("ALIVE_DB_PATH", "alive_history.db")
WEEKLY_REPORT_DAY = os.environ.get("ALIVE_WEEKLY_DAY", "mon").lower()
WEEKLY_REPORT_HOUR = int(os.environ.get("ALIVE_WEEKLY_HOUR", "9"))

REQUIRED_EMAIL_VARS = {
    "EMAIL_FROM": EMAIL_FROM,
    "EMAIL_TO": EMAIL_TO,
    "SMTP_SERVER": SMTP_SERVER,
    "SMTP_PORT": SMTP_PORT_RAW,
    "SMTP_USER": SMTP_USER,
    "SMTP_PASS": SMTP_PASS,
}

def check_email_config() -> bool:
    missing = [k for k, v in REQUIRED_EMAIL_VARS.items() if not v]
    if missing:
        logger.error(
            "Missing SMTP environment configuration in `.env`. Required fields or their ALERTS_ prefixes: %s",
            ", ".join(missing),
        )
        return False
    return True

# ---------------------------------------------------------------------------
# SQLite Database Helpers & History Tracking
# ---------------------------------------------------------------------------
def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_PATH) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                server_name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                is_alive INTEGER NOT NULL,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

def log_check_results(all_servers_status: list[dict], db_path: str = DB_PATH) -> None:
    init_db(db_path)
    now_str = dt.datetime.now(dt.timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        for s in all_servers_status:
            conn.execute(
                """
                INSERT INTO checks (timestamp, server_name, host, port, is_alive, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_str, s["name"], s["host"], s["port"], 1 if s["is_alive"] else 0, s.get("error")),
            )
        conn.commit()

def get_weekly_stats(db_path: str = DB_PATH, days: int = 7) -> dict:
    init_db(db_path)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    
    stats = {}
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT server_name, host, port, is_alive, error, timestamp
            FROM checks
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (cutoff,),
        ).fetchall()
        
        for r in rows:
            name = r["server_name"]
            if name not in stats:
                stats[name] = {
                    "name": name,
                    "host": r["host"],
                    "port": r["port"],
                    "total_checks": 0,
                    "success_count": 0,
                    "fail_count": 0,
                    "last_status": None,
                    "failures": [],
                }
            
            stats[name]["total_checks"] += 1
            if r["is_alive"]:
                stats[name]["success_count"] += 1
            else:
                stats[name]["fail_count"] += 1
                stats[name]["failures"].append({
                    "timestamp": r["timestamp"],
                    "error": r["error"]
                })
            
            stats[name]["last_status"] = {
                "is_alive": bool(r["is_alive"]),
                "timestamp": r["timestamp"],
                "error": r["error"]
            }
            
    for s in stats.values():
        total = s["total_checks"]
        s["uptime_pct"] = (s["success_count"] / total * 100.0) if total > 0 else 0.0
        
    return stats

# ---------------------------------------------------------------------------
# Email helpers (Alert & Weekly Status Email)
# ---------------------------------------------------------------------------
def send_alert_email(failed_servers: list[dict], all_servers_status: list[dict]) -> None:
    if not check_email_config():
        logger.error("Skipping email alert due to missing email configuration.")
        return

    subject = f"⚠️ SSH Server Alert: {len(failed_servers)} server(s) not alive"
    
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    body = f"SSH Server Status Report (checked on {now_str})\n"
    body += "=" * 50 + "\n\n"
    
    body += "The following server(s) failed the aliveness check:\n"
    for s in failed_servers:
        body += f"❌ {s['name']} ({s['host']}:{s['port']}) - Error: {s['error']}\n"
    
    body += "\n" + "=" * 50 + "\n\n"
    body += "Complete Server Status list:\n"
    for s in all_servers_status:
        status_icon = "✅" if s["is_alive"] else "❌"
        status_text = "Success" if s["is_alive"] else f"Failed ({s['error']})"
        body += f"{status_icon} {s['name']} ({s['host']}:{s['port']}) - {status_text}\n"
        
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        logger.info("📧 Sent email alert: %s", subject)
    except smtplib.SMTPException:
        logger.exception("SMTP failure sending SSH server alert")
    except Exception:
        logger.exception("Unexpected error sending email")

def send_weekly_status_email(force: bool = False, db_path: str = DB_PATH) -> None:
    if not check_email_config():
        logger.error("Skipping weekly email report due to missing email configuration.")
        return

    stats = get_weekly_stats(db_path=db_path, days=7)
    servers = load_monitored_servers()
    
    all_server_names = set(s["name"] for s in servers) | set(stats.keys())
    
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    subject = f"📊 SSH Server Weekly Status Report ({date_str})"
    
    body = f"SSH Server Weekly Status Report ({now_str})\n"
    body += "=" * 65 + "\n\n"
    
    if not stats and not servers:
        body += "No servers configured and no check history in the past 7 days.\n"
    else:
        body += "Server Summary (Past 7 Days):\n"
        body += "-" * 65 + "\n"
        body += f"{'Server Name':<20} | {'Uptime %':<10} | {'Checks':<8} | {'Passed/Failed':<13} | {'Current'}\n"
        body += "-" * 65 + "\n"
        
        for name in sorted(all_server_names):
            if name in stats:
                st = stats[name]
                uptime_str = f"{st['uptime_pct']:.1f}%"
                checks_str = str(st['total_checks'])
                pass_fail = f"{st['success_count']}/{st['fail_count']}"
                current_icon = "✅ Alive" if (st['last_status'] and st['last_status']['is_alive']) else "❌ Down"
                body += f"{st['name']:<20} | {uptime_str:<10} | {checks_str:<8} | {pass_fail:<13} | {current_icon}\n"
            else:
                body += f"{name:<20} | {'N/A':<10} | {'0':<8} | {'0/0':<13} | {'No Data'}\n"
                
        body += "\n" + "=" * 65 + "\n\n"
        
        has_failures = any(st.get("fail_count", 0) > 0 for st in stats.values())
        if has_failures:
            body += "Recent Incident Log (Past 7 Days):\n"
            body += "-" * 65 + "\n"
            for name, st in stats.items():
                if st["failures"]:
                    body += f"\nServer: {name} ({st['host']}:{st['port']})\n"
                    for f in st["failures"][-10:]:
                        ts = f['timestamp']
                        err = f['error'] or 'Unknown error'
                        body += f"  • [{ts}] {err}\n"
        else:
            body += "🎉 No outages recorded in the past 7 days! All checks passed.\n"
            
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        logger.info("📧 Sent weekly status email report: %s", subject)
        
        with get_db_connection(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_weekly_email', ?)",
                (dt.datetime.now(dt.timezone.utc).isoformat(),)
            )
            conn.commit()
    except smtplib.SMTPException:
        logger.exception("SMTP failure sending weekly status email")
    except Exception:
        logger.exception("Unexpected error sending weekly status email")

def check_and_send_weekly_email_if_due(db_path: str = DB_PATH) -> None:
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'last_weekly_email'").fetchone()
        
    if not row:
        stats = get_weekly_stats(db_path)
        if stats:
            logger.info("First run with check history recorded. Sending weekly email report...")
            send_weekly_status_email(force=True, db_path=db_path)
    else:
        try:
            last_sent = dt.datetime.fromisoformat(row["value"])
            days_since = (dt.datetime.now(dt.timezone.utc) - last_sent).total_seconds() / 86400.0
            if days_since >= 7.0:
                logger.info("7 days elapsed since last weekly status email (%.1f days). Sending weekly report...", days_since)
                send_weekly_status_email(force=True, db_path=db_path)
        except Exception as e:
            logger.warning("Error parsing last_weekly_email timestamp: %s", e)

# ---------------------------------------------------------------------------
# Server configuration parser
# ---------------------------------------------------------------------------
def load_monitored_servers() -> list[dict]:
    servers_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".servers_env")
    if not os.path.exists(servers_env_path):
        logger.warning("`.servers_env` file not found. Creating a blank template.")
        with open(servers_env_path, "w") as f:
            f.write(
                "# SSH Server monitoring configuration\n"
                "# Define servers with prefix SERVER_<NAME>_\n"
                "# At minimum HOST is required.\n"
                "# Example:\n"
                "# SERVER_EXAMPLE_HOST=127.0.0.1\n"
                "# SERVER_EXAMPLE_PORT=22\n"
                "# SERVER_EXAMPLE_USER=ubuntu\n"
                "# SERVER_EXAMPLE_KEY_PATH=~/.ssh/id_rsa\n"
            )
        return []

    config = dotenv_values(servers_env_path)
    servers = {}
    for key, value in config.items():
        if not key.startswith("SERVER_") or not value:
            continue
            
        parts = key.split("_")
        if len(parts) < 3:
            continue
            
        field = parts[-1].upper()
        name = "_".join(parts[1:-1])
        
        if name not in servers:
            servers[name] = {"name": name, "port": 22}
            
        if field in ("HOST", "IP"):
            servers[name]["host"] = value
        elif field == "PORT":
            try:
                servers[name]["port"] = int(value)
            except ValueError:
                pass
        elif field in ("USER", "USERNAME"):
            servers[name]["user"] = value
        elif field in ("PASS", "PASSWORD"):
            servers[name]["password"] = value
        elif field in ("KEY_PATH", "KEY"):
            servers[name]["key_path"] = value

    valid_servers = [s for s in servers.values() if "host" in s]
    return valid_servers

# ---------------------------------------------------------------------------
# SSH Check connection logic
# ---------------------------------------------------------------------------
def check_ssh_server(server: dict) -> tuple[bool, str]:
    host = server["host"]
    port = server["port"]
    user = server.get("user")
    password = server.get("password")
    key_path = server.get("key_path")
    name = server["name"]

    logger.info("Checking server %s (%s:%d)...", name, host, port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        connect_kwargs = {
            "hostname": host,
            "port": port,
            "timeout": 20,
            "banner_timeout": 20,
        }
        if user:
            connect_kwargs["username"] = user
        if password:
            connect_kwargs["password"] = password
        if key_path:
            expanded_key_path = os.path.expanduser(key_path)
            if os.path.exists(expanded_key_path):
                connect_kwargs["key_filename"] = expanded_key_path
            else:
                return False, f"Key file not found: {key_path}"

        client.connect(**connect_kwargs)
        
        stdin, stdout, stderr = client.exec_command("echo 'alive'", timeout=5)
        output = stdout.read().decode().strip()
        if output == "alive":
            return True, "Success"
        else:
            return False, f"Unexpected connection check output: '{output}'"
            
    except paramiko.AuthenticationException as e:
        return False, f"Authentication failed: {e}"
    except paramiko.SSHException as e:
        return False, f"SSH connection error: {e}"
    except socket.timeout:
        return False, "Connection timed out"
    except socket.error as e:
        return False, f"Network error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
    finally:
        client.close()

# ---------------------------------------------------------------------------
# Core monitoring function
# ---------------------------------------------------------------------------
def run_checks(db_path: str = DB_PATH) -> None:
    poll_mins = get_poll_interval_minutes()
    logger.info("Starting SSH servers aliveness check [v%s] (Poll interval: %d mins)...", VERSION, poll_mins)
    servers = load_monitored_servers()
    
    if not servers:
        logger.warning("No servers defined in `.servers_env`. Skipping check.")
        return
        
    failed_servers = []
    all_servers_status = []
    
    for server in servers:
        is_alive, msg = check_ssh_server(server)
        status_info = {
            "name": server["name"],
            "host": server["host"],
            "port": server["port"],
            "is_alive": is_alive,
            "error": msg if not is_alive else None
        }
        all_servers_status.append(status_info)
        
        if is_alive:
            logger.info("✅ Server %s is alive.", server["name"])
        else:
            logger.error("❌ Server %s is NOT alive: %s", server["name"], msg)
            failed_servers.append(status_info)
            
    log_check_results(all_servers_status, db_path=db_path)
    
    if failed_servers:
        logger.info("Found %d unavailable server(s). Sending alert email...", len(failed_servers))
        send_alert_email(failed_servers, all_servers_status)
    else:
        logger.info("All checked servers are alive. No alert email sent.")
        
    check_and_send_weekly_email_if_due(db_path=db_path)

def get_poll_interval_minutes() -> int:
    servers_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".servers_env")
    val = None
    if os.path.exists(servers_env_path):
        cfg = dotenv_values(servers_env_path)
        val = cfg.get("POLL_INTERVAL_MINUTES") or cfg.get("POLL_INTERVAL_SEC") or cfg.get("POLL_INTERVAL")
    
    if not val:
        val = os.environ.get("POLL_INTERVAL_MINUTES") or os.environ.get("POLL_INTERVAL_SEC") or os.environ.get("POLL_INTERVAL") or "60"
        
    try:
        ival = int(val)
        if ival >= 60 and ("SEC" in str(val).upper() or ival >= 300):
            return max(1, ival // 60)
        return max(1, ival)
    except ValueError:
        return 60

# ---------------------------------------------------------------------------
# Main & Scheduler Execution
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="SSH Server Aliveness Monitor")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a background daemon service checking at specified poll interval and sending weekly reports",
    )
    parser.add_argument(
        "--weekly-report",
        action="store_true",
        help="Send the weekly status email report immediately",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    args = parser.parse_args()
    
    poll_mins = get_poll_interval_minutes()
    logger.info("SSH Server Monitor v%s (Configured Poll Interval: %d mins)", VERSION, poll_mins)

    if args.weekly_report:
        logger.info("Manual trigger: Sending weekly status email report...")
        send_weekly_status_email(force=True)
        return

    if args.daemon:
        from apscheduler.schedulers.background import BackgroundScheduler
        
        sched = BackgroundScheduler()
        
        # Schedule check job every poll_mins minutes starting immediately
        sched.add_job(
            run_checks,
            "interval",
            minutes=poll_mins,
            next_run_time=dt.datetime.now(),
            misfire_grace_time=3600
        )
        
        # Schedule weekly report job with misfire grace time
        sched.add_job(
            send_weekly_status_email,
            "cron",
            day_of_week=WEEKLY_REPORT_DAY,
            hour=WEEKLY_REPORT_HOUR,
            minute=0,
            misfire_grace_time=86400,
        )
        
        sched.start()
        logger.info(
            "SSH Server Monitor v%s started in daemon mode. Polling interval: every %d minute(s). Weekly report scheduled (%s at %02d:00).",
            VERSION, poll_mins, WEEKLY_REPORT_DAY.upper(), WEEKLY_REPORT_HOUR
        )
        
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down SSH Server Monitor...")
            sched.shutdown()
    else:
        # Single-run mode
        run_checks()

if __name__ == "__main__":
    main()
