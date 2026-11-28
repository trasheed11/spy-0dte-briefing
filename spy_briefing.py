import os
import math
import smtplib
import time
import ssl
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

AZ_TZ = ZoneInfo("America/Phoenix")
NY_TZ = ZoneInfo("America/New_York")
MAX_PREMARKET_AGE_MINUTES = 20


def valid_price(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid market price")
    return value


def previous_daily_row(history, as_of):
    """Select a daily bar strictly before the briefing's New York date."""
    if history.empty:
        raise ValueError("No daily history returned")
    history = history.sort_index()
    idx = history.index
    if idx.tz is not None:
        idx = idx.tz_convert(NY_TZ)
    prior = history.loc[idx.date < as_of.astimezone(NY_TZ).date()]
    if prior.empty:
        raise ValueError("No previous completed daily session returned")
    stamp = prior.index[-1]
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(NY_TZ)
    return prior.iloc[-1], stamp.date()


def premarket_snapshot(history, as_of):
    """Use only completed same-date 04:00–09:30 ET minute bars."""
    local = as_of.astimezone(NY_TZ)
    opening = local.replace(hour=9, minute=30, second=0, microsecond=0)
    beginning = local.replace(hour=4, minute=0, second=0, microsecond=0)
    if local.weekday() >= 5:
        return None, "UNAVAILABLE (weekend; no same-day regular premarket session)"
    if local < beginning:
        return None, "UNAVAILABLE (before regular premarket session)"
    if history.empty:
        return None, "UNAVAILABLE (no intraday history returned)"
    history = history.sort_index().copy()
    if history.index.tz is None:
        return None, "UNAVAILABLE (intraday timestamps have no timezone)"
    history.index = history.index.tz_convert(NY_TZ)
    cutoff = min(local, opening)
    bar_end = history.index + timedelta(minutes=1)
    rows = history.loc[
        (history.index >= beginning)
        & (history.index < opening)
        & (bar_end <= cutoff)
    ]
    rows = rows.dropna(subset=["Close"])
    if rows.empty:
        return None, "UNAVAILABLE (no completed same-day premarket bar)"
    last_end = rows.index[-1] + timedelta(minutes=1)
    age_minutes = (cutoff - last_end).total_seconds() / 60.0
    stamp_az = last_end.astimezone(AZ_TZ).strftime("%Y-%m-%d %I:%M %p MST")
    if age_minutes > MAX_PREMARKET_AGE_MINUTES:
        return None, f"UNAVAILABLE (last premarket bar ended {stamp_az}; stale at reference time, {age_minutes:.1f} min old)"
    try:
        price = valid_price(rows["Close"].iloc[-1])
    except (TypeError, ValueError):
        return None, "UNAVAILABLE (invalid premarket price)"
    if local >= opening:
        status = f"Historical premarket snapshot; bar ended {stamp_az}; {age_minutes:.1f} min before regular open; NOT a current regular-session quote"
    else:
        status = f"Premarket snapshot; bar ended {stamp_az}; {age_minutes:.1f} min old at retrieval; feed may be delayed"
    return price, status


def dollars(value, signed=False):
    if value is None:
        return "UNAVAILABLE"
    return f"${value:+.2f}" if signed else f"${value:.2f}"


def main():
    import requests
    import yfinance as yf
    from google import genai
    from google.genai import errors

    # 1. Setup Arizona time.
    now_az = datetime.now(AZ_TZ)
    today_str = now_az.strftime("%Y-%m-%d")
    display_date = now_az.strftime("%A, %b %d, %Y")

    # 2. Market data: explicit sessions, source timestamps, and no silent fallback.
    spy = yf.Ticker("SPY")
    vix = yf.Ticker("^VIX")
    spy_hist = spy.history(period="5d", interval="1d", auto_adjust=False, raise_errors=True)
    previous_row, previous_date = previous_daily_row(spy_hist, now_az)
    prev_close = valid_price(previous_row["Close"])
    pdh = valid_price(previous_row["High"])
    pdl = valid_price(previous_row["Low"])
    if not pdl <= prev_close <= pdh:
        raise ValueError("Previous SPY OHLC values are inconsistent")

    premarket_price = None
    try:
        intraday = spy.history(period="5d", interval="1m", prepost=True, auto_adjust=False, raise_errors=True)
        premarket_price, premarket_status = premarket_snapshot(intraday, datetime.now(AZ_TZ))
    except Exception as exc:
        premarket_status = f"UNAVAILABLE (intraday retrieval error: {type(exc).__name__})"

    # Previous completed VIX close, explicitly labeled; never claim it is live.
    vix_level = None
    try:
        vix_hist = vix.history(period="5d", interval="1d", auto_adjust=False, raise_errors=True)
        vix_row, vix_date = previous_daily_row(vix_hist, now_az)
        vix_level = valid_price(vix_row["Close"])
        vix_text = f"{vix_level:.2f} (previous completed VIX close, {vix_date}; not a live quote)"
    except Exception as exc:
        vix_text = f"UNAVAILABLE (VIX retrieval error: {type(exc).__name__})"

    # 3. Calculate levels only when the required inputs are available.
    gap_dollar = gap_pct = expected_move = upper_em = lower_em = None
    if premarket_price is not None:
        gap_dollar = premarket_price - prev_close
        gap_pct = (gap_dollar / prev_close) * 100
        if vix_level is not None:
            expected_move = premarket_price * (vix_level / 100.0) / math.sqrt(252)
            upper_em = premarket_price + expected_move
            lower_em = premarket_price - expected_move

    gap_text = (f"{dollars(gap_dollar, signed=True)} ({gap_pct:+.2f}%)" if gap_pct is not None else "UNAVAILABLE")
    em_text = f"±${expected_move:.2f}" if expected_move is not None else "UNAVAILABLE"
    checked_at = datetime.now(AZ_TZ).strftime("%Y-%m-%d %I:%M:%S %p MST")
    market_data = f"""SOURCE DATA: Yahoo Finance via yfinance (may be delayed)
Data checked at: {checked_at}
- SPY Premarket Price: {dollars(premarket_price)}
- SPY Premarket Data Status: {premarket_status}
- Previous Close (Gap Fill Target): {dollars(prev_close)} (session {previous_date})
- Overnight Gap: {gap_text}
- Previous Day High (PDH): {dollars(pdh)} (session {previous_date})
- Previous Day Low (PDL): {dollars(pdl)} (session {previous_date})
- VIX: {vix_text}
- Approximate VIX-based daily move (centered on premarket snapshot): {em_text}
- Upper Expected Move Reference: {dollars(upper_em)}
- Lower Expected Move Reference: {dollars(lower_em)}
The expected move is a rough volatility estimate, not an options-implied forecast or a guaranteed boundary.
"""
    print(market_data)

    # 4. Existing Finnhub economic calendar and news retrieval.
    finnhub_key = os.environ["FINNHUB_API_KEY"]
    cal_url = "https://finnhub.io/api/v1/calendar/economic"
    cal_events = []
    try:
        cal_response = requests.get(cal_url, params={"from": today_str, "to": today_str}, headers={"X-Finnhub-Token": finnhub_key}, timeout=10)
        cal_response.raise_for_status()
        cal_data = cal_response.json().get("economicCalendar", [])
        for ev in cal_data:
            if ev.get("country") == "US" and ev.get("impact") in ["high", "medium"]:
                utc_time_str = ev.get("time", "")
                az_time_display = utc_time_str
                try:
                    dt_utc = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                    az_time_display = dt_utc.astimezone(AZ_TZ).strftime("%I:%M %p MST")
                except Exception:
                    pass
                cal_events.append(f"- [{ev.get('impact').upper()}] {az_time_display}: {ev.get('event')} (Est: {ev.get('estimate')}, Prev: {ev.get('prev')})")
    except Exception as e:
        cal_events.append(f"- Could not load calendar: {e}")

    calendar_text = "\n".join(cal_events) if cal_events else "- No major high/medium US economic releases scheduled today."

    news_url = "https://finnhub.io/api/v1/news"
    headlines = []
    try:
        news_response = requests.get(news_url, params={"category": "general"}, headers={"X-Finnhub-Token": finnhub_key}, timeout=10)
        news_response.raise_for_status()
        news_data = news_response.json()[:8]
        for item in news_data:
            headlines.append(f"- {item.get('headline')}")
    except Exception:
        pass
    headlines_text = "\n".join(headlines) if headlines else "- No major overnight headlines fetched."

    # 5. Existing Gemini analyst with explicit missing-data handling.
    weekend = now_az.astimezone(NY_TZ).weekday() >= 5
    weekend_note = "This is a weekend diagnostic run. Clearly label the email as a test; do not give live trade triggers." if weekend else "This is a weekday briefing; verify market hours independently before trading."
    prompt = f"""
You are a disciplined institutional SPY 0DTE pre-market analyst writing a morning game plan for a trader in Arizona (MST) trading a $900 E*TRADE Cash Account.

Here is the supplied market data for {display_date}:
{market_data}

DATA INTEGRITY RULES:
- Use supplied numeric values exactly. Do not invent, replace, or infer an unavailable quote, gap, VIX level, or expected move.
- A previous close is historical, never a current premarket price. A historical premarket snapshot after the opening bell is not a current quote.
- If a required input is unavailable, label any dependent analysis unavailable or conditional. Do not give a flat-gap conclusion when the gap is unknown.
- A calendar retrieval failure means releases are unverified, not that none are scheduled.
- Expected move references are approximate, not hard support/resistance or a guarantee.
- {weekend_note}

TODAY'S US ECONOMIC CALENDAR (ARIZONA MST TIMES):
{calendar_text}

OVERNIGHT MARKET HEADLINES:
{headlines_text}

Write a crisp, high-signal SPY 0DTE Morning Battle Plan formatted cleanly for email with these exact sections:
1. DANGER ZONES & NEWS TIMING: Highlight any verified high-impact economic releases today with exact MST times and remind the trader to flatten or avoid entries 5 minutes before/after those timestamps. Mention any key overnight macro theme from the headlines.
2. KEY CHART LEVELS TO MARK: List available Upper Expected Move, PDH, Pre-Market Price, Previous Close, PDL, and Lower Expected Move from highest to lowest price, noting what each level means. Explicitly label missing levels.
3. VOLATILITY & GAP REGIME: Explain what the supplied VIX and overnight gap may mean, with appropriate uncertainty. Do not infer a current volatility regime from missing or historical-only data.
4. EXACT IF / THEN ENTRY TRIGGERS:
   - Bullish CALL Setup (using PDH breakout/retest, ORB, or Lower Expected Move bounce with exact price levels if available).
   - Bearish PUT Setup (using PDH liquidity sweep rejection, PDL breakdown, or Upper Expected Move fade with exact price levels if available).
   - Make all scenarios conditional. Do not give live entry triggers on weekends or when critical data is missing.
5. $900 CASH ACCOUNT DISCIPLINE: Remind the trader to use conservative position sizing, ATM or 1-strike OTM contracts, a maximum 20% premium-loss risk ceiling and earlier exits on structural invalidation. Verify exchange close time and avoid new entries in the final hour. Do not assume a fixed Arizona cutoff on all dates.
"""

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(3):
        try:
            response = client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt)
            briefing_body = response.text
            if not briefing_body or not briefing_body.strip():
                raise RuntimeError("Gemini returned an empty briefing")
            break
        except errors.ServerError as exc:
            if exc.code != 503 or attempt == 2:
                raise
            delay = (20, 60)[attempt]
            print(f"Gemini busy. Retrying in {delay} seconds.")
            time.sleep(delay)

    # 6. Existing Gmail delivery; raw source data included before the AI analysis.
    sender_email = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = sender_email
    prefix = "[WEEKEND TEST] " if weekend else ""
    msg["Subject"] = f"{prefix}SPY 0DTE Battle Plan ({display_date}) | Pre: {dollars(premarket_price)} | EM: {em_text}"
    msg.attach(MIMEText(market_data + "\n" + "=" * 50 + "\n\n" + briefing_body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(sender_email, app_password)
        server.send_message(msg)
    print("SPY 0DTE Briefing sent successfully!")


if __name__ == "__main__":
    main()
