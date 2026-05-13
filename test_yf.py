import yfinance as yf
import datetime as dt

ticker = "QBTS"
daily = yf.Ticker(ticker).history(period="2d", interval="1d", auto_adjust=False)
print("daily")
print(daily)

today = dt.datetime.now().date()
minute = yf.Ticker(ticker).history(
    start=today.isoformat(),
    end=(today + dt.timedelta(days=1)).isoformat(),
    interval="1m",
    auto_adjust=False,
)
print("minute")
if not minute.empty:
    print(minute.iloc[-1])
