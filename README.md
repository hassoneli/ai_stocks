# ai_stocks
```bash
python3 -m venv venv
source venv/bin/activate
pip install yfinance apscheduler python-dotenv tzdata paramiko
```

## SSH Server Monitor (`alive.py`)

A script that monitors SSH servers continuously, logs aliveness history to SQLite (`alive_history.db`), emails instant alerts when servers fail, and sends a weekly status report email summarizing uptime and incidents.

### Configuration
1. **Email Settings**: Handled in `.env` (using `ALERTS_*` variables).
2. **Servers & Polling**: Configured in `.servers_env`:
   ```env
   # Poll Interval in minutes (default: 60 = check every hour)
   POLL_INTERVAL_MINUTES=60

   SERVER_VPN_HOST=egvpnsrv.ddns.net
   SERVER_VPN_PORT=22
   SERVER_VPN_USER=egvpnsrv
   SERVER_VPN_PASS=password123
   ```

### Execution
Simply run `alive.py` directly — it will stay running in a continuous polling loop:

* **Default Polling Mode** (polls continuously every `POLL_INTERVAL_MINUTES` minutes without external cron):
  ```bash
  python alive.py
  ```
* **Single Check Run** (runs once and exits):
  ```bash
  python alive.py --once
  ```
* **Weekly Report Trigger** (send the 7-day status report on demand):
  ```bash
  python alive.py --weekly-report
  ```
