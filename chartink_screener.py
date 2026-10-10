import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
import os
import json
import time

import firebase_admin
from firebase_admin import credentials, firestore

# GitHub Secrets
EMAIL = os.getenv('EMAIL')
APP_PASSWORD = os.getenv('APP_PASSWORD')
FIREBASE_SERVICE_ACCOUNT = os.getenv('FIREBASE_SERVICE_ACCOUNT')


def load_scans():
    """Load scans from external JSON"""
    try:
        with open('scans.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ scans.json missing! Create it first.")
        return []


def load_settings():
    """Load shared dashboard and scan-history settings."""
    with open('settings.json', 'r', encoding='utf-8') as f:
        settings = json.load(f)

    history_days = settings.get('history_days')
    if isinstance(history_days, bool) or not isinstance(history_days, int) or history_days < 1:
        raise ValueError("settings.json must define history_days as a positive integer")
    return settings


SETTINGS = load_settings()
SCANS = load_scans()
print(f"📋 Loaded {len(SCANS)} scans from scans.json")


def fetch_chartink_scan(scan_name, scan_clause):
    """Fetch results for ONE scan"""
    session = requests.Session()
    resp = session.get("https://chartink.com/screener")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    csrf_token = soup.select_one('meta[name="csrf-token"]')['content']

    payload = {"scan_clause": scan_clause}
    session.headers["X-CSRF-TOKEN"] = csrf_token
    session.headers["Content-Type"] = "application/x-www-form-urlencoded"

    r = session.post("https://chartink.com/screener/process", data=payload)
    r.raise_for_status()

    data = r.json()
    if not data.get("data") or len(data["data"]) == 0:
        return []

    num_cols = len(data["data"][0])
    df = pd.DataFrame(data["data"])
    headers = ["S.No", "SYMBOL", "NAME", "", "% from 52WH", "% from 52WL", "Price"][:num_cols]
    df.columns = headers
    return df["SYMBOL"].tolist()


def run_all_scans():
    """Run ALL scans"""
    all_results = {}
    print("🔍 Running multi-scan...")

    for scan in SCANS:
        print(f"  📊 {scan['name']}")
        symbols = fetch_chartink_scan(scan['name'], scan['clause'])
        all_results[scan['name']] = symbols
        print(f"     {len(symbols)} stocks")

    return all_results


def send_multi_scan_email(all_results):
    """Clean email format - Just bold scan names + stocks"""
    if not EMAIL or not APP_PASSWORD:
        print("⚠️ No email secrets")
        return False

    body_lines = [f"\n"]

    for scan_name, symbols in all_results.items():
        body_lines.append(f"\n{'='*80}")
        body_lines.append(f"**{scan_name}** ({len(symbols)} stocks)")
        body_lines.append(f"{'='*80}")

        if symbols:
            for symbol in symbols[:20]:
                body_lines.append(f"{symbol}")
            if len(symbols) > 20:
                body_lines.append(f"... and {len(symbols)-20} more")
        else:
            body_lines.append("No stocks matching criteria today")

    body = "\n".join(body_lines)

    msg = MIMEText(body, 'plain')
    msg['Subject'] = f'Scan Report - {datetime.now().strftime("%Y-%m-%d")}'
    msg['From'] = EMAIL
    msg['To'] = EMAIL

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("✅ CLEAN EMAIL SENT!")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


def cleanup_old_scans(db, days_to_keep=SETTINGS['history_days']):
    """Delete dated scan docs older than `days_to_keep` days. Leaves
    the 'latest' doc untouched."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d')
    deleted = 0

    docs = db.collection('scans').where('date', '<', cutoff).stream()
    for d in docs:
        if d.id == 'latest':
            continue
        d.reference.delete()
        deleted += 1

    if deleted:
        print(f"🧹 Removed {deleted} scan doc(s) older than {days_to_keep} days")


YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _empty_returns():
    return {"price": None, "day": None, "week": None, "month": None, "three_month": None}


def fetch_symbol_history(symbol):
    """Pull ~4 months of daily closes for one NSE symbol from Yahoo's
    chart API directly."""
    period2 = int(datetime.now().timestamp())
    period1 = int((datetime.now() - timedelta(days=120)).timestamp())

    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
        f"?period1={period1}&period2={period2}&interval=1d&events=history"
    )
    resp = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("chart", {}).get("result")
    if not result:
        return []

    r = result[0]
    timestamps = r.get("timestamp", [])
    closes = r["indicators"]["quote"][0].get("close", [])

    # pair up, dropping any nulls (non-trading days / gaps in the response)
    return [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]


def compute_returns(symbols):
    """For each symbol, fetch price history and compute % return over
    several lookback windows. Returns {symbol: {"day":, "week":,
    "month":, "three_month":}} — values are None if a fetch/calc failed
    for that symbol, so one bad stock doesn't break the rest."""
    if not symbols:
        return {}

    print(f"📈 Fetching price history for {len(symbols)} symbols...")
    returns = {}

    for symbol in symbols:
        try:
            series = fetch_symbol_history(symbol)
            if not series:
                returns[symbol] = _empty_returns()
                continue

            latest_ts, latest_close = series[-1]

            def pct_change_from(days_back):
                target_ts = latest_ts - days_back * 86400
                past_candidates = [c for ts, c in series if ts <= target_ts]
                if not past_candidates:
                    return None
                past_close = past_candidates[-1]
                if past_close == 0:
                    return None
                return round(((latest_close - past_close) / past_close) * 100, 2)

            day_return = (
                round(((latest_close - series[-2][1]) / series[-2][1]) * 100, 2)
                if len(series) >= 2 and series[-2][1] else None
            )

            returns[symbol] = {
                "price": round(latest_close, 2),
                "day": day_return,
                "week": pct_change_from(7),
                "month": pct_change_from(30),
                "three_month": pct_change_from(90),
            }
        except Exception as e:
            print(f"  ⚠️ {symbol}: couldn't fetch/compute returns ({e})")
            returns[symbol] = _empty_returns()

        time.sleep(0.15)  # small pacing gap between requests, easy on Yahoo's rate limits

    return returns


def push_to_firestore(all_results):
    """Push today's scan results to Firestore so the GitHub Pages
    dashboard can read them. Writes both a dated doc (history) and a
    'latest' doc (what the page loads by default)."""
    if not FIREBASE_SERVICE_ACCOUNT:
        print("⚠️ No FIREBASE_SERVICE_ACCOUNT secret — skipping Firestore push")
        return False

    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT))
            firebase_admin.initialize_app(cred)

        db = firestore.client()

        now = datetime.now()
        today = now.strftime('%Y-%m-%d')

        # dedupe symbols across scans so we only fetch each stock's history once
        unique_symbols = sorted({sym for symbols in all_results.values() for sym in symbols})
        returns_map = compute_returns(unique_symbols)

        doc_data = {
            "date": today,
            "timestamp": now.strftime('%Y-%m-%d %H:%M:%S'),
            "scans": {
                scan_name: {
                    "count": len(symbols),
                    "symbols": symbols,
                }
                for scan_name, symbols in all_results.items()
            },
            "returns": returns_map,
        }

        # dated doc = history, 'latest' = what the dashboard reads
        db.collection('scans').document(today).set(doc_data)
        db.collection('scans').document('latest').set(doc_data)

        print("✅ Pushed results to Firestore")

        cleanup_old_scans(db, days_to_keep=30)
        return True
    except Exception as e:
        print(f"❌ Firestore push failed: {e}")
        return False


# MAIN EXECUTION
all_results = run_all_scans()
push_to_firestore(all_results)
today_str = datetime.now().strftime('%Y-%m-%d')
print(f"🕐 Multi-Scan: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")

if datetime.now().weekday() >= 5:
    print("⏭️ Weekend — skipping")
else:
    
    # send_multi_scan_email(all_results)  # disabled — data now lives in Firestore / the dashboard
    
    print("🎉 Complete!")