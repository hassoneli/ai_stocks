# ai_stocks
python3 -m venv venv
source venv/bin/activate
pip install yfinance apscheduler python-dotenv tzdata paramiko

## SSH Server Monitor (`alive.py`)

A script that monitors SSH servers and emails you if any of them are down.

### Configuration
1. **Email Settings**: Handled in `.env` (using the same `ALERTS_*` variables as `pm.py`).
2. **Servers**: Configured in `.servers_env` using the following format:
   ```env
   SERVER_MYSERVER_HOST=192.168.1.100
   SERVER_MYSERVER_PORT=22
   SERVER_MYSERVER_USER=ubuntu
   SERVER_MYSERVER_PASS=password123 # (Optional: password)
   SERVER_MYSERVER_KEY_PATH=~/.ssh/id_rsa # (Optional: key file path)
   ```

### Execution
* **Single Check Run** (useful for standard cron jobs running once a day):
  ```bash
  python alive.py
  ```
* **Daemon Scheduler Mode** (runs in the background, checking once a day at a scheduled hour/minute. Hour/minute can be set via `ALIVE_CHECK_HOUR` and `ALIVE_CHECK_MINUTE` in your `.env`):
  ```bash
  python alive.py --daemon
  ```
