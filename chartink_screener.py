import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import os
import json

# GitHub Secrets
EMAIL = os.getenv('EMAIL')
APP_PASSWORD = os.getenv('APP_PASSWORD')

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
    soup = BeautifulSoup(resp.text, "html.parser")  # ✅ Fixed parser
    csrf_token = soup.select_one('meta[name="csrf-token"]')['content']
    
    payload = {"scan_clause": scan_clause}  # ✅ Uses scan_clause parameter
    session.headers["X-CSRF-TOKEN"] = csrf_token
    session.headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    r = session.post("https://chartink.com/screener/process", data=payload)
    r.raise_for_status()
    
    data = r.json()
    if not data.get("data") or len(data["data"]) == 0:
        return []
    
    num_cols = len(data["data"][0])
    df = pd.DataFrame(data["data"])
    headers = ["S.No", "SYMBOL", "NAME", "","% from 52WH", "% from 52WL", "Price"][:num_cols]
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
            for symbol in symbols[:20]:  # Top 20 stocks
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


# MAIN EXECUTION
print(f"🕐 Multi-Scan: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")

if 0 <= datetime.now().weekday():
    all_results = run_all_scans()
    send_multi_scan_email(all_results)
    print("🎉 Complete!")
else:
    print("⏭️ Weekend")
