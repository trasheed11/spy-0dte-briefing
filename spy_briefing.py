import os
import math
import smtplib
import requests
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# 1. Setup Arizona Time (MST)
az_tz = ZoneInfo("America/Phoenix")
now_az = datetime.now(az_tz)
today_str = now_az.strftime("%Y-%m-%d")
display_date = now_az.strftime("%A, %b %d, %Y")

# 2. Fetch SPY & VIX Market Data via Yahoo Finance
spy = yf.Ticker("SPY")
vix = yf.Ticker("^VIX")

spy_hist = spy.history(period="5d")
vix_hist = vix.history(period="5d")

prev_close = float(spy_hist["Close"].iloc[-1])
pdh = float(spy_hist["High"].iloc[-1])
pdl = float(spy_hist["Low"].iloc[-1])

# Grab current pre-market price (falls back to prev_close if unavailable)
spy_info = spy.fast_info
premarket_price = float(getattr(spy_info, "last_price", prev_close) or prev_close)
vix_level = float(getattr(vix.fast_info, "last_price", vix_hist["Close"].iloc[-1]))

# 3. Calculate Exact Math Levels
gap_dollar = premarket_price - prev_close
gap_pct = (gap_dollar / prev_close) * 100

# VIX-Implied Daily Expected Move: Price * (VIX / 100) / sqrt(252)
expected_move = premarket_price * (vix_level / 100.0) / math.sqrt(252)
upper_em = premarket_price + expected_move
lower_em = premarket_price - expected_move

# 4. Fetch Economic Calendar & Market News from Finnhub
finnhub_key = os.environ["FINNHUB_API_KEY"]

# Economic Calendar for today
cal_url = f"https://finnhub.io{today_str}&to={today_str}&token={finnhub_key}"
cal_events = []
try:
    cal_data = requests.get(cal_url, timeout=10).json().get("economicCalendar", [])
    for ev in cal_data:
        if ev.get("country") == "US" and ev.get("impact") in ["high", "medium"]:
            # Convert UTC event time to Arizona (MST)
            utc_time_str = ev.get("time", "")
            az_time_display = utc_time_str
            try:
                dt_utc = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                az_time_display = dt_utc.astimezone(az_tz).strftime("%I:%M %p MST")
            except Exception:
                pass
            cal_events.append(f"- [{ev.get('impact').upper()}] {az_time_display}: {ev.get('event')} (Est: {ev.get('estimate')}, Prev: {ev.get('prev')})")
except Exception as e:
    cal_events.append(f"- Could not load calendar: {e}")

calendar_text = "\n".join(cal_events) if cal_events else "- No major high/medium US economic releases scheduled today."

# Top General Market Headlines
news_url = f"https://finnhub.io{finnhub_key}"
headlines = []
try:
    news_data = requests.get(news_url, timeout=10).json()[:8]
    for item in news_data:
        headlines.append(f"- {item.get('headline')}")
except Exception:
    pass
headlines_text = "\n".join(headlines) if headlines else "- No major overnight headlines fetched."

# 5. Build Prompt for Gemini AI Analyst
prompt = f"""
You are a disciplined institutional SPY 0DTE pre-market analyst writing a morning game plan for a trader in Arizona (MST) trading a $900 E*TRADE Cash Account.

Here is the exact market data as of {now_az.strftime('%I:%M %p MST')} on {display_date}:
- SPY Pre-Market Price: ${premarket_price:.2f}
- Previous Close (Gap Fill Target): ${prev_close:.2f}
- Overnight Gap: ${gap_dollar:+.2f} ({gap_pct:+.2f}%)
- Previous Day High (PDH): ${pdh:.2f}
- Previous Day Low (PDL): ${pdl:.2f}
- VIX Level: {vix_level:.2f}
- VIX-Implied Daily Expected Move: ±${expected_move:.2f}
- Upper Expected Move Boundary: ${upper_em:.2f}
- Lower Expected Move Boundary: ${lower_em:.2f}

TODAY'S US ECONOMIC CALENDAR (ARIZONA MST TIMES):
{calendar_text}

OVERNIGHT MARKET HEADLINES:
{headlines_text}

Write a crisp, high-signal SPY 0DTE Morning Battle Plan formatted cleanly for email with these exact sections:
1. DANGER ZONES & NEWS TIMING: Highlight any high-impact economic releases today with exact MST times and remind the trader to flatten or avoid entries 5 minutes before/after those timestamps. Mention any key overnight macro theme from the headlines.
2. KEY CHART LEVELS TO MARK: List Upper Expected Move, PDH, Pre-Market Price, Previous Close, PDL, and Lower Expected Move from highest to lowest price, noting what each level means.
3. VOLATILITY & GAP REGIME: Explain what today's VIX ({vix_level:.2f}) and overnight gap ({gap_pct:+.2f}%) mean (e.g., likely gap-fill chop vs. trend breakout, and how fast Theta will decay).
4. EXACT IF / THEN ENTRY TRIGGERS:
   - Bullish CALL Setup (using PDH breakout/retest, ORB, or Lower Expected Move bounce with exact price levels).
   - Bearish PUT Setup (using PDH liquidity sweep rejection, PDL breakdown, or Upper Expected Move fade with exact price levels).
5. $900 CASH ACCOUNT DISCIPLINE: Remind the trader to size entries at $300–$450 max (2–3 clean bullets), stick to ATM or 1-strike OTM contracts, use a strict -20% stop-loss, and avoid new entries after 11:30 AM MST.
"""

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
)
briefing_body = response.text

# 6. Send the Email via Gmail
sender_email = os.environ["GMAIL_ADDRESS"]
app_password = os.environ["GMAIL_APP_PASSWORD"]

msg = MIMEMultipart()
msg["From"] = sender_email
msg["To"] = sender_email
msg["Subject"] = f"SPY 0DTE Battle Plan ({display_date}) | Pre: ${premarket_price:.2f} | EM: ±${expected_move:.2f}"
msg.attach(MIMEText(briefing_body, "plain"))

with smtplib.SMTP_SSL("://gmail.com", 465) as server:
    server.login(sender_email, app_password)
    server.send_message(msg)

print("SPY 0DTE Briefing sent successfully!")
