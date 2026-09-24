import os
import logging
import datetime
import io
import threading
import time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import yfinance as yf
from flask import Flask
import telebot
from pymongo import MongoClient
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# --- 1. Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ProdTradingBot")

# --- 2. MongoDB Setup ---
MONGO_URI = os.environ.get("MONGO_URI")
db = None
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI)
        db = client["trading_bot_db"]
        signals_collection = db["signals"]
        logger.info("MongoDB Connected Successfully!")
    except Exception as e:
        logger.error(f"MongoDB Connection Failed: {e}")
else:
    logger.warning("MONGO_URI not set. Running without persistent database.")

def save_signal_to_db(ticker, date, decision):
    if db is not None:
        try:
            signals_collection.insert_one({
                "ticker": ticker,
                "date": date,
                "decision": decision,
                "timestamp": datetime.datetime.now()
            })
            logger.info(f"Signal for {ticker} saved to MongoDB.")
        except Exception as e:
            logger.error(f"MongoDB Insert Error: {e}")

# --- 3. TradingAgents Setup (Using Official Gemini 2.5 Flash) ---
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-2.5-flash"
config["quick_think_llm"] = "gemini-2.5-flash"
config["use_social_sentiment"] = False
config["max_retries"] = 2

try:
    ta = TradingAgentsGraph(debug=False, config=config)
    logger.info("TradingAgents Initialized with Gemini 2.5 Flash.")
except Exception as e:
    logger.error(f"Failed to initialize TradingAgents: {e}")
    ta = None

# --- 4. Chart Generation ---
def generate_chart(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1mo")
        if hist.empty:
            return None
        plt.figure(figsize=(10, 5))
        plt.plot(hist.index, hist['Close'], label='Close Price', color='#00ffaa')
        plt.title(f'{ticker} - 1 Month Price Trend')
        plt.grid(True, linestyle='--', alpha=0.5)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()
        return buf
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
        return None

# --- 5. Telegram Bot Setup ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is missing from Environment Variables!")

bot = telebot.TeleBot(BOT_TOKEN)

try:
    bot.remove_webhook()
    bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhooks cleared successfully.")
except Exception as e:
    logger.warning(f"Webhook cleanup warning: {e}")

# --- 6. Telegram Handlers (Accepts both uppercase and lowercase) ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Namaste! Main aapka AI Trading Assistant hoon.\n\n"
        "Stock Signal: `/signal AAPL` ya `/signal RELIANCE.NS`\n"
        "Stock Chart: `/chart TSLA` ya `/chart TATAMOTORS.NS`\n"
        "History: `/history`\n\n"
        "*(Note: Ek analysis mein 30-60 second lag sakte hain)*"
    )
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(func=lambda msg: msg.text and msg.text.split()[0].lower() in ['/signal', '/signal@alphabot_crypto_bot'])
def handle_signal(message):
    if ta is None:
        bot.reply_to(message, "System abhi ready nahi hai. Thodi der baad prayas karein.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Kripya stock symbol dein. Example: `/signal NVDA`", parse_mode='Markdown')
            return
        
        ticker = parts[1].upper().replace(" ", "").strip()
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        
        bot.reply_to(message, f"AI Agents '{ticker}' ko analyze kar rahe hain... Kripya 30-60 second wait karein.")
        
        _, decision = ta.propagate(ticker, current_date)
        save_signal_to_db(ticker, current_date, str(decision))
        
        response_text = f"Analysis Report for {ticker}:\n\n{decision}"
        if len(response_text) > 4000:
            for i in range(0, len(response_text), 4000):
                bot.send_message(message.chat.id, response_text[i:i+4000])
        else:
            bot.reply_to(message, response_text)
    except Exception as e:
        logger.error(f"Signal error: {e}")
        bot.reply_to(message, f"Analysis complete nahi ho paya. Reason: {str(e)[:150]}")

@bot.message_handler(func=lambda msg: msg.text and msg.text.split()[0].lower() in ['/chart', '/chart@alphabot_crypto_bot'])
def handle_chart(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Kripya stock symbol dein. Example: `/chart TSLA`", parse_mode='Markdown')
            return
        
        ticker = parts[1].upper().replace(" ", "").strip()
        bot.reply_to(message, f"{ticker} ka chart generate ho raha hai...")
        chart_buf = generate_chart(ticker)
        if chart_buf:
            bot.send_photo(message.chat.id, chart_buf, caption=f"Chart: {ticker} (Last 1 Month)")
        else:
            bot.reply_to(message, f"{ticker} ka market data nahi mila. Stock symbol check karein.")
    except Exception as e:
        bot.reply_to(message, f"Chart error: {e}")

@bot.message_handler(commands=['history'])
def handle_history(message):
    if db is None:
        bot.reply_to(message, "Database connected nahi hai.")
        return
    try:
        rows = signals_collection.find().sort("timestamp", -1).limit(5)
        text = "Last 5 Saved Signals:\n\n"
        count = 0
        for row in rows:
            text += f"• {row.get('ticker')} ({row.get('date')})\n"
            count += 1
        if count == 0:
            bot.reply_to(message, "Abhi tak koi signal save nahi hua.")
        else:
            bot.reply_to(message, text)
    except Exception as e:
        bot.reply_to(message, "History fetch nahi ho saki.")

# --- 7. Telegram Background Polling ---
def start_polling():
    logger.info("Starting Telegram Polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            logger.error(f"Polling warning: {e}")
            time.sleep(5)

threading.Thread(target=start_polling, daemon=True).start()

# --- 8. Health Check for Render ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Trading Bot is 100% Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
