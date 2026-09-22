import os
import logging
import datetime
import io
import threading
import matplotlib
matplotlib.use('Agg')  # Server पर बिना GUI के चार्ट बनाने के लिए
import matplotlib.pyplot as plt
import yfinance as yf
from flask import Flask, request, abort
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
    logger.warning("⚠️ MONGO_URI not set. Database features will be disabled.")

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

# --- 3. TradingAgents Configuration & Groq Fallback ---
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-2.5-flash"
config["quick_think_llm"] = "gemini-2.5-flash"
config["use_social_sentiment"] = False
config["max_retries"] = 3

try:
    ta = TradingAgentsGraph(debug=False, config=config)
    logger.info("✅ TradingAgents Initialized with Gemini.")
except Exception as e:
    logger.error(f"Failed to initialize TradingAgents: {e}")
    ta = None

def run_analysis_with_fallback(ticker, date):
    """Runs analysis using Gemini, falls back to Groq if Gemini fails."""
    global ta
    try:
        # Try with Gemini first
        _, decision = ta.propagate(ticker, date)
        return decision
    except Exception as e:
        logger.warning(f"Gemini failed for {ticker}: {e}. Switching to Groq...")
        try:
            # Fallback to Groq
            fallback_config = config.copy()
            fallback_config["llm_provider"] = "groq"
            fallback_config["deep_think_llm"] = "llama-3.3-70b-versatile"
            fallback_config["quick_think_llm"] = "llama-3.3-70b-versatile"
            
            fallback_ta = TradingAgentsGraph(debug=False, config=fallback_config)
            _, decision = fallback_ta.propagate(ticker, date)
            logger.info(f"✅ Groq Fallback Successful for {ticker}")
            return decision
        except Exception as fallback_error:
            logger.error(f"❌ Groq Fallback also failed: {fallback_error}")
            raise Exception("Both Gemini and Groq failed to analyze.")

# --- 4. Chart Generation ---
def generate_chart(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1mo")
        if hist.empty:
            return None
        plt.figure(figsize=(10, 5))
        plt.plot(hist.index, hist['Close'], label='Close Price', color='blue')
        plt.title(f'{ticker} - Last 1 Month')
        plt.xlabel('Date')
        plt.ylabel('Price')
        plt.grid(True)
        plt.legend()
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        return buf
    except Exception as e:
        logger.error(f"Chart generation failed for {ticker}: {e}")
        return None

# --- 5. Telegram Bot Setup ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") # e.g., https://my-app.onrender.com
bot = telebot.TeleBot(BOT_TOKEN)

# --- 6. Flask App for Webhook ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Trading Bot is Running!", 200

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

# --- 7. Telegram Message Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = ("🙏 *Namaste! Main aapka Advanced AI Trading Assistant hoon.*\n\n"
            "📈 *Signal lene ke liye:* `/signal AAPL`\n"
            "📊 *Chart dekhne ke liye:* `/chart TSLA`\n"
            "📜 *Pichle 5 signals dekhne ke liye:* `/history`\n"
            "⏪ *Backtest karne ke liye:* `/backtest AAPL 2024-01-01`")
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
        bot.reply_to(message, "❌ Analysis fail ho gaya. Kripya symbol check karein.")

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

@bot.message_handler(commands=['backtest'])
def handle_backtest(message):
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, "⚠️ Example: `/backtest AAPL 2024-01-01`", parse_mode='Markdown')
            return
        ticker = parts[1].upper().strip()
        past_date = parts[2]
        bot.reply_to(message, f"⏪ *Backtesting {ticker} for {past_date}...*\nAI puraane data ko analyze kar raha hai. Kripya wait karein.", parse_mode='Markdown')
        
        # Simple Backtest: Just run the AI on the past date and see what it would have said
        decision = run_analysis_with_fallback(ticker, past_date)
        response_text = f"⏪ *Backtest Report for {ticker} ({past_date})*\n\n{decision}"
        bot.reply_to(message, response_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Backtest Error: {e}")
        bot.reply_to(message, "❌ Backtest fail ho gaya.")

# --- 8. Start Server / Webhook / Polling ---
if __name__ == "__main__":
    if WEBHOOK_URL:
        logger.info("Setting up Webhook...")
        bot.remove_webhook()
        bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
        logger.info(f"✅ Webhook set to {WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        logger.info("No WEBHOOK_URL found, starting Polling...")
        # Start polling in a separate thread so Flask can run
        threading.Thread(target=bot.infinity_polling, kwargs={'timeout': 60, 'long_polling_timeout': 30}, daemon=True).start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
