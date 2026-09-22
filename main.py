import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# Render ka Port Error fix karne ke liye dummy server
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), BaseHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# Gemini aur Trading Agents Configuration
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-2.5-flash"
config["quick_think_llm"] = "gemini-2.5-flash"

# StockTwits aur Reddit ke 403/429 block se bachne ke liye
config["use_social_sentiment"] = False

# Agents Graph initialize karein
ta = TradingAgentsGraph(debug=True, config=config)

# NVDA share ka signal test karein
_, decision = ta.propagate("NVDA", "2026-09-01")
print("\n=== FINAL TRADING SIGNAL ===")
print(decision)
