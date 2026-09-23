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
from telebot import types
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
        logger.info("✅ MongoDB Connected Successfully!")
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Failed: {e}")
else:
    logger.warning("⚠️ MONGO_URI not set.")

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

# --- 3. TradingAgents Configuration (UPDATED MODELS) ---
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
# ✅ अब gemini-2.0-flash इस्तेमाल करें, यह स्टेबल है
config["deep_think_llm"] = "gemini-2.0-flash"
config["quick_think_llm"] = "gemini-2.0-flash"
config["use_social_sentiment"] = False
config["max_retries"] = 2

try:
    ta = TradingAgentsGraph(debug=False, config=config)
    logger.info("✅ TradingAgents Initialized with Gemini 2.0 Flash.")
except Exception as e:
    logger.error(f"Failed to initialize TradingAgents: {e}")
    ta = None

def run_analysis_with_fallback(ticker, date):
    """Gemini फेल होने पर Groq के दो मॉडल्स को आजमाता है"""
    global ta
    # 1. पहले Gemini से कोशिश करें
    try:
        _, decision = ta.propagate(ticker, date)
        return decision
    except Exception as e:
        logger.warning(f"⚠️ Gemini failed for {ticker}: {e}. Switching to Groq...")
    
    # 2. अब Groq पर स्विच करें
    groq_models = [
        ("groq", "openai/gpt-oss-120b"),      # पहला विकल्प
        ("groq", "llama-3.1-8b-instant")      # दूसरा विकल्प
    ]
    
    for provider, model_name in groq_models:
        try:
            fallback_config = config.copy()
            fallback_config["llm_provider"] = provider
            fallback_config["deep_think_llm"] = model_name
            fallback_config["quick_think_llm"] = model_name
            
            fallback_ta = TradingAgentsGraph(debug=False, config=fallback_config)
            _, decision = fallback_ta.propagate(ticker, date)
            logger.info(f"✅ Groq Fallback Successful with {model_name} for {ticker}")
            return decision
        except Exception as fallback_error:
            logger.error(f"❌ Groq model {model_name} failed: {fallback_error}")
            continue # अगला मॉडल ट्राई करें
    
    # अगर सब फेल हो जाएं
    raise Exception("All Gemini and Groq models failed. Please check API keys and quotas.")

# --- 4. Chart Generation ---
def generate_chart(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1mo")
        if hist.empty: return None
        plt.figure(figsize=(10, 5))
        plt.plot(hist.index, hist['Close'], label='Close Price', color='blue')
        plt.title(f'{ticker} - Last 1 Month')
        plt.grid(True)
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        return buf
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
        return None

# --- 5. Telegram Bot Setup ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

try:
    bot.remove_webhook()
    bot.delete_webhook(drop_pending_updates=True) 
    logger.info("✅ Old Webhooks and Pending Updates cleared.")
except Exception as e:
    logger.warning(f"Webhook cleanup failed: {e}")

# --- 6. Telegram Message Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = ("🙏 *Namaste! Main aapka Advanced AI Trading Assistant hoon.*\n\n"
            "📈 *Signal lene ke liye:* `/signal AAPL`\n"
            "📊 *Chart dekhne ke liye:* `/chart TSLA`\n"
            "📜 *Pichle 5 signals dekhne ke liye:* `/history`")
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['signal'])
def handle_signal(message):
    if ta is None:
        bot.reply_to(message, "❌ System initialize nahi ho paya.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Example: `/signal RELIANCE.NS`", parse_mode='Markdown')
            return
        ticker = parts[1].upper().strip()
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        
        bot.reply_to(message, f"⏳ *AI Agents '{ticker}' ko analyze kar rahe hain...*\nKripya 30-60 second wait karein.", parse_mode='Markdown')
        
        decision = run_analysis_with_fallback(ticker, current_date)
        save_signal_to_db(ticker, current_date, decision)
        
        response_text = f"📊 *Analysis Report for {ticker}*\n\n{decision}"
        if len(response_text) > 4000:
            for i in range(0, len(response_text), 4000):
                bot.send_message(message.chat.id, response_text[i:i+4000], parse_mode='Markdown')
        else:
            bot.reply_to(message, response_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Signal Error: {e}")
        bot.reply_to(message, f"❌ Analysis fail ho gaya. Reason: {str(e)[:100]}")

@bot.message_handler(commands=['chart'])
def handle_chart(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Example: `/chart NVDA`", parse_mode='Markdown')
            return
        ticker = parts[1].upper().strip()
        bot.reply_to(message, f"📈 {ticker} ka chart ban raha hai...")
        chart_buf = generate_chart(ticker)
        if chart_buf:
            bot.send_photo(message.chat.id, chart_buf, caption=f"📊 {ticker} - 1 Month Chart")
        else:
            bot.reply_to(message, f"❌ {ticker} ka data nahi mila.")
    except Exception as e:
        bot.reply_to(message, "❌ Chart generate nahi ho paya.")

@bot.message_handler(commands=['history'])
def handle_history(message):
    if db is None:
        bot.reply_to(message, "❌ Database connected nahi hai.")
        return
    try:
        rows = signals_collection.find().sort("timestamp", -1).limit(5)
        text = "📜 *Last 5 Signals (MongoDB):*\n\n"
        count = 0
        for row in rows:
            text += f"🔹 *{row['ticker']}* on {row['date']}\n"
            count += 1
        if count == 0:
            bot.reply_to(message, "📭 Abhi tak koi signal save nahi hua.")
        else:
            bot.reply_to(message, text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, "❌ History fetch karne mein error aaya.")

# --- 7. Start Polling in a Background Thread (ROBUST RETRY) ---
def start_polling():
    logger.info("🚀 Starting Telegram Polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                logger.error("⚠️ 409 Conflict: Another instance is running. Retrying in 10 seconds...")
                time.sleep(10)
            else:
                logger.error(f"❌ Telegram API Error: {e}")
                time.sleep(5)
        except Exception as e:
            logger.error(f"❌ Unexpected polling error: {e}")
            time.sleep(5)

threading.Thread(target=start_polling, daemon=True).start()

# --- 8. Flask App for Health Check ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Trading Bot is Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
