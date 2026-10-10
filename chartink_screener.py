import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
import os
import json
import re
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
    price_history_days = settings.get('price_history_days')
    if (
        isinstance(price_history_days, bool)
        or not isinstance(price_history_days, int)
        or price_history_days < max(90, history_days)
    ):
        raise ValueError(
            "settings.json must define price_history_days as an integer of at least "
            "90 and no less than history_days"
        )
    return settings


SETTINGS = load_settings()
SCANS = load_scans()
print(f"📋 Loaded {len(SCANS)} scans from scans.json")


def fetch_chartink_scan(scan_name, scan_clause):
    """Fetch symbols and their prices for ONE scan."""
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
        return [], {}

    rows = data["data"]
    symbols = []
    prices = {}

    for row in rows:
        if isinstance(row, dict):
            symbol = row.get("nsecode")
            price = row.get("close")
        else:
            # Support the older positional response format as well.
            if len(row) < 2:
                raise ValueError(f"Unexpected Chartink row for scan {scan_name!r}: {row!r}")
            symbol = row[1]
            price = row[6] if len(row) > 6 else None

        if not symbol:
            continue
        symbols.append(symbol)
        if price is not None:
            prices[symbol] = float(price)

    return symbols, prices


def run_all_scans():
    """Run ALL scans"""
    all_results = {}
    chartink_prices = {}
    print("🔍 Running multi-scan...")

    for scan in SCANS:
        print(f"  📊 {scan['name']}")
        symbols, prices = fetch_chartink_scan(scan['name'], scan['clause'])
        all_results[scan['name']] = symbols
        chartink_prices.update(prices)
        print(f"     {len(symbols)} stocks")

    return all_results, chartink_prices


def fetch_chartink_prices(symbols):
    """Fetch current Chartink prices for symbols, including those no longer matching a scan."""
    if not symbols:
        return {}

    session = requests.Session()
    page = session.get("https://chartink.com/screener", timeout=20)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    token_element = soup.select_one('meta[name="csrf-token"]')
    if token_element is None or not token_element.get("content"):
        raise ValueError("Chartink response did not contain a CSRF token")
    session.headers.update({
        "X-CSRF-TOKEN": token_element["content"],
        "Content-Type": "application/x-www-form-urlencoded",
    })

    prices = {}
    for symbol in sorted(set(symbols)):
        if not re.fullmatch(r"[A-Z0-9&.-]+", symbol):
            print(f"  ⚠️ Skipping unsupported Chartink symbol {symbol!r}")
            continue

        clause = f"( {{cash}} ( symbol = '{symbol}' ) )"
        response = session.post(
            "https://chartink.com/screener/process",
            data={"scan_clause": clause},
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json().get("data", [])
        for row in rows:
            if isinstance(row, dict) and row.get("nsecode") == symbol and row.get("close") is not None:
                prices[symbol] = float(row["close"])
                break
        else:
            print(f"  ⚠️ Chartink returned no price for {symbol}")

        time.sleep(0.15)

    return prices


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


def cleanup_old_scans(db, days_to_keep=SETTINGS['price_history_days']):
    """Delete dated scan docs older than `days_to_keep` days. Leaves
    the 'latest' doc untouched."""
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


def _empty_returns():
    return {"price": None, "day": None, "week": None, "month": None, "three_month": None}


def compute_returns(current_prices, history_docs, as_of):
    """Compute returns from Chartink price snapshots stored in Firestore."""
    returns = {}
    previous_prices = []
    for doc in history_docs:
        doc_date = doc.get("date")
        if not doc_date or doc_date >= as_of:
            continue
        previous_prices.append((doc_date, doc.get("latest_prices", {})))
    previous_prices.sort(key=lambda item: item[0], reverse=True)

    for symbol, current_price in current_prices.items():
        symbol_returns = _empty_returns()
        symbol_returns["price"] = round(current_price, 2)

        for field, days_back in (("day", 1), ("week", 7), ("month", 30), ("three_month", 90)):
            target_date = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%Y-%m-%d")
            past_price = next(
                (
                    prices[symbol]
                    for doc_date, prices in previous_prices
                    if doc_date <= target_date and symbol in prices
                ),
                None,
            )
            if past_price is not None and past_price != 0:
                symbol_returns[field] = round(((current_price - past_price) / past_price) * 100, 2)

        returns[symbol] = symbol_returns

    return returns


def push_to_firestore(all_results, chartink_prices):
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

        cutoff_date = (now - timedelta(days=SETTINGS['price_history_days'])).strftime('%Y-%m-%d')
        history_docs = [
            doc.to_dict()
            for doc in db.collection('scans').where('date', '>=', cutoff_date).stream()
            if doc.id != 'latest'
            and doc.id != today
            and cutoff_date <= (doc.to_dict().get('date') or '') < today
        ]

        tracked_symbols = {
            symbol
            for scan_data in all_results.values()
            for symbol in scan_data
        }
        for historical_doc in history_docs:
            tracked_symbols.update((historical_doc.get('latest_prices') or {}).keys())
            for scan_data in (historical_doc.get('scans') or {}).values():
                tracked_symbols.update(scan_data.get('symbols') or [])

        missing_prices = tracked_symbols.difference(chartink_prices)
        if missing_prices:
            print(f"📈 Fetching Chartink prices for {len(missing_prices)} previously scanned symbols...")
            chartink_prices.update(fetch_chartink_prices(missing_prices))

        returns_map = compute_returns(chartink_prices, history_docs, today)

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
            "latest_prices": chartink_prices,
            "price_source": "chartink",
        }

        # dated doc = history, 'latest' = what the dashboard reads
        db.collection('scans').document(today).set(doc_data)
        db.collection('scans').document('latest').set(doc_data)

        print("✅ Pushed results to Firestore")

        cleanup_old_scans(db)
        return True
    except Exception as e:
        print(f"❌ Firestore push failed: {e}")
        return False


# MAIN EXECUTION
all_results, chartink_prices = run_all_scans()
push_to_firestore(all_results, chartink_prices)
today_str = datetime.now().strftime('%Y-%m-%d')
print(f"🕐 Multi-Scan: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")

if datetime.now().weekday() >= 5:
    print("⏭️ Weekend — skipping")
else:
    
    # send_multi_scan_email(all_results)  # disabled — data now lives in Firestore / the dashboard
    
    print("🎉 Complete!")