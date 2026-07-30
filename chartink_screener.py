import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import os
import json

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


def cleanup_old_scans(db, days_to_keep=30):
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
today_str = datetime.now().strftime('%Y-%m-%d')
print(f"🕐 Multi-Scan: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")

if datetime.now().weekday() >= 5:
    print("⏭️ Weekend — skipping")
else:
    all_results = run_all_scans()
    # send_multi_scan_email(all_results)  # disabled — data now lives in Firestore / the dashboard
    push_to_firestore(all_results)
    print("🎉 Complete!")