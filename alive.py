#!/usr/bin/env python3
"""
SSH Server Aliveness Monitor
---------------------------
Connects to SSH servers defined in `.servers_env` once a day (in daemon mode)
or instantly (in single-run mode), and sends an email alert if one or more of
them are not alive.

Required environment variables in `.env` (for email alerts):
    ALERTS_EMAIL_FROM       Sender address
    ALERTS_EMAIL_TO         Recipient address
    ALERTS_SMTP_SERVER      SMTP host
    ALERTS_SMTP_PORT        SMTP SSL port (465)
    ALERTS_SMTP_USER        SMTP username
    ALERTS_SMTP_PASS        SMTP password / app‑password

SSH servers are configured in `.servers_env` with patterns like:
    SERVER_PROD_HOST=192.168.1.10
    SERVER_PROD_PORT=22
    SERVER_PROD_USER=ubuntu
    SERVER_PROD_KEY_PATH=~/.ssh/id_rsa
    
Usage:
    # Run once immediately (useful for testing or cron jobs)
    python alive.py
    
    # Run as a daemon/service that checks once a day
    python alive.py --daemon
"""

import argparse
import datetime as dt
import logging
import os
import socket
import smtplib
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
# Email helper
# ---------------------------------------------------------------------------
def send_alert_email(failed_servers: list[dict], all_servers_status: list[dict]) -> None:
    if not check_email_config():
        logger.error("Skipping email alert due to missing email configuration.")
        return

    subject = f"⚠️ SSH Server Alert: {len(failed_servers)} server(s) not alive"
    
    # Build email body
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

# ---------------------------------------------------------------------------
# Server configuration parser
# ---------------------------------------------------------------------------
def load_monitored_servers() -> list[dict]:
    servers_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".servers_env")
    if not os.path.exists(servers_env_path):
        logger.warning("`.servers_env` file not found. Creating a blank template.")
        # Create a default template file if it doesn't exist
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
        # Extract the server name/id in the middle, preserving inner underscores
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

    # Filter out entries that lack a host
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
            "timeout": 10,
            "banner_timeout": 10,
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
        
        # Test command execution to verify shell is active
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
def run_checks() -> None:
    logger.info("Starting SSH servers aliveness check...")
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
            
    if failed_servers:
        logger.info("Found %d unavailable server(s). Sending alert email...", len(failed_servers))
        send_alert_email(failed_servers, all_servers_status)
    else:
        logger.info("All checked servers are alive. No alert email sent.")

# ---------------------------------------------------------------------------
# Main & Scheduler Execution
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="SSH Server Aliveness Monitor")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a daemon/service that checks once a day using APScheduler",
    )
    args = parser.parse_args()
    
    if args.daemon:
        from apscheduler.schedulers.background import BackgroundScheduler
        
        # Configure check time from environment if specified (default 09:00 local time)
        hour = int(os.environ.get("ALIVE_CHECK_HOUR", "9"))
        minute = int(os.environ.get("ALIVE_CHECK_MINUTE", "0"))
        
        sched = BackgroundScheduler()
        
        # Schedule the job to run daily at the specified hour:minute
        sched.add_job(run_checks, "cron", hour=hour, minute=minute)
        
        # Trigger an immediate check on startup so the daemon can be verified
        sched.add_job(run_checks, "date", run_date=dt.datetime.now())
        
        sched.start()
        logger.info(
            "SSH Server Monitor started in daemon mode. Scheduling daily checks at %02d:%02d local time.",
            hour, minute
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
