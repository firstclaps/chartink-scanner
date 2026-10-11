import yfinance as yf

price = yf.Ticker("RELIANCE.NS").fast_info["last_price"]
print(price)