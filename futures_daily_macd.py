import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import os

# GitHub Secrets
EMAIL = os.getenv('EMAIL')
APP_PASSWORD = os.getenv('APP_PASSWORD')

def fetch_chartink_macd_screener():
    session = requests.Session()
    resp = session.get("https://chartink.com/screener")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    csrf_token = soup.select_one('meta[name="csrf-token"]')['content']
    
    payload = {"scan_clause": SCAN_CLAUSE}
    session.headers["X-CSRF-TOKEN"] = csrf_token
    session.headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    r = session.post("https://chartink.com/screener/process", data=payload)
    r.raise_for_status()
    
    data = r.json()
    if not data.get("data") or len(data["data"]) == 0:
        print("No stocks found")
        return pd.DataFrame()
    
    num_cols = len(data["data"][0])
    df = pd.DataFrame(data["data"])
    headers = ["S.No", "SYMBOL", "NAME", "","% from 52WH", "% from 52WL", "Price"][:num_cols]
    df.columns = headers
    return df

def send_email(symbols):
    if not EMAIL or not APP_PASSWORD:
        print("⚠️ No email secrets - CSV saved only")
        return False
    
    body = f"🔔 Daily Futures MACD ({len(symbols)} stocks) - {datetime.now().strftime('%Y-%m-%d')}\n\n" + "\n".join(f"• {s}" for s in symbols)
    
    msg = MIMEText(body)
    msg['Subject'] = f'Futures MACD Signals - {datetime.now().strftime("%Y-%m-%d")}'
    msg['From'] = EMAIL
    msg['To'] = EMAIL
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("✅ EMAIL SENT!")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False

# MAIN EXECUTION
print(f"🕐 Running Futures MACD Scanner: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")

if 0 <= datetime.now().weekday() <= 4:  # Mon-Fri only
    df = fetch_chartink_macd_screener()
    
    if not df.empty:
        symbols = df["SYMBOL"].tolist()
        print(f"📈 Found {len(symbols)} stocks: {symbols[:5]}...")  # First 5
        
        send_email(symbols)
    else:
        print("❌ No MACD crossovers today")
else:
    print("⏭️ Weekend - Skipping")
