import yfinance as yf
import datetime as dt
import pytz

NY_TZ = pytz.timezone("America/New_York")
now_ny = dt.datetime.now(tz=NY_TZ)
today = now_ny.date()
ticker = "QBTS"

daily = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
past_daily = daily[daily.index.date < today]
prev_close = float(past_daily["Close"].iloc[-1])

print(f"prev_close: {prev_close}")
