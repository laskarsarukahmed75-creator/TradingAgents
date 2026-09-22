import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# --- 1. Logging Setup (Production Level) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("TradingBot")

# --- 2. Render Health-Check Server (Fixes HEAD request 501 error) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"TradingAgents Bot is Running!")
        
    def do_HEAD(self):
        # Render uses HEAD requests to check if the service is alive
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress health check spam in logs
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check server running on port {port}")
    server.serve_forever()

# Start Health Server in a daemon thread
threading.Thread(target=run_health_server, daemon=True).start()

# --- 3. TradingAgents Configuration (Optimized for Production) ---
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-2.5-flash"   # Stable model
config["quick_think_llm"] = "gemini-2.5-flash"  # Stable model
config["use_social_sentiment"] = False          # Fixes Reddit/StockTwits 403/429 errors
config["max_retries"] = 3                       # Retry if Gemini API fails temporarily

logger.info("Initializing TradingAgents Graph...")
try:
    ta = TradingAgentsGraph(debug=False, config=config)
    logger.info("TradingAgents Graph initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize TradingAgents: {e}")
    ta = None

# --- 4. Telegram Bot Setup ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "🙏 *Namaste! Main aapka Trading Assistant hoon.*\n\n"
        "Kisi bhi Stock, Index, ya ETF ka signal lene ke liye command bhejein:\n"
        "👉 `/signal AAPL`\n"
        "👉 `/signal ^NSEI` (Nifty 50)\n"
        "👉 `/signal ^NSEBANK` (Bank Nifty)\n"
        "👉 `/signal GC=F` (Gold)\n\n"
        "⚠️ *Dhyan rakhein:* Analysis mein 30-60 second lag sakte hain."
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['signal'])
def handle_signal(message):
    if ta is None:
        bot.reply_to(message, "❌ System error: Trading engine initialize nahi ho paya. Kripya logs check karein.")
        return

    try:
        # Extract ticker from command (e.g., /signal NVDA)
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Kripya stock symbol likhein. Example: `/signal TSLA`", parse_mode='Markdown')
            return
        
        ticker = parts[1].upper().strip()
        logger.info(f"Received signal request for: {ticker} from user {message.chat.id}")

        # Send initial processing message
        processing_msg = bot.reply_to(
            message, 
            f"⏳ *6 AI Agents '{ticker}' ko analyze kar rahe hain...*\nKripya 30-60 second wait karein.", 
            parse_mode='Markdown'
        )

        # Run Analysis
        try:
            _, decision = ta.propagate(ticker, "2026-09-01")
            
            # Format the output
            response_text = f"📊 *Analysis Report for {ticker}*\n\n{decision}"
            
            # Telegram has a 4096 character limit per message, split if necessary
            if len(response_text) > 4000:
                for i in range(0, len(response_text), 4000):
                    bot.send_message(message.chat.id, response_text[i:i+4000], parse_mode='Markdown')
            else:
                bot.reply_to(message, response_text, parse_mode='Markdown')
                
            logger.info(f"Successfully sent signal for {ticker}")

        except Exception as e:
            logger.error(f"Analysis failed for {ticker}: {str(e)}")
            bot.reply_to(message, f"❌ *Analysis Error:* {ticker} ka data fetch nahi ho paya. Kripya symbol sahi format mein likhein (jaise: RELIANCE.NS, ^NSEI).", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in handle_signal: {str(e)}")
        bot.reply_to(message, "❌ Kuch technical error aa gaya hai. Kripya thodi der baad try karein.")

# --- 5. Start Polling ---
logger.info("Bot is polling and ready for commands...")
bot.infinity_polling(timeout=60, long_polling_timeout=30)
