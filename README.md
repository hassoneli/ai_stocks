# ai_stocks
```bash
python3 -m venv venv
source venv/bin/activate
pip install yfinance apscheduler python-dotenv tzdata paramiko
```

## SSH Server Monitor (`alive.py`)

A script that monitors SSH servers hourly, logs aliveness history to SQLite (`alive_history.db`), emails instant alerts when servers fail, and sends a weekly status report email summarizing uptime and incidents.

### Configuration
1. **Email Settings**: Handled in `.env` (using `ALERTS_*` variables).
2. **Database & Report Schedule** (optional in `.env`):
   ```env
   ALIVE_DB_PATH=alive_history.db
   ALIVE_WEEKLY_DAY=mon  # mon, tue, wed, thu, fri, sat, sun
   ALIVE_WEEKLY_HOUR=9   # 0-23
   ```
3. **Servers**: Configured in `.servers_env` using the following format:
   ```env
   SERVER_MYSERVER_HOST=192.168.1.100
   SERVER_MYSERVER_PORT=22
   SERVER_MYSERVER_USER=ubuntu
   SERVER_MYSERVER_PASS=password123 # (Optional: password)
   SERVER_MYSERVER_KEY_PATH=~/.ssh/id_rsa # (Optional: key file path)
   ```

### Execution Modes
* **Single Check Run**:
  ```bash
  python alive.py
  ```
* **Weekly Report Trigger** (send the 7-day status report on demand):
  ```bash
  python alive.py --weekly-report
  ```
* **Daemon Scheduler Mode** (runs continuously in background, checking hourly at minute 0 and sending weekly status reports every Monday at 9:00 AM):
  ```bash
  python alive.py --daemon
  ```

### Running on Termux / POCO (Android proot-distro)
On Termux / proot containers, standard system `cron` services are often not running in the background. Instead, use the built-in daemon mode:

1. Start background daemon runner:
   ```bash
   ./start_daemon.sh
   ```
2. Optional (Termux wake lock to prevent Android OS sleeping process):
   ```bash
   termux-wake-lock
   ```

### Standard Linux Crontab Setup
```cron
0 * * * * /home/eli/projects/ai_stocks/venv/bin/python /home/eli/projects/ai_stocks/alive.py >> /home/eli/projects/ai_stocks/alive.log 2>&1
```
