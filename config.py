import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = os.getenv("PAPER_DB", "paper_arena.db")

# --- market impact model -------------------------------------------------
# Angel One gives 5 levels of depth. Beyond that, synthetic levels are generated
# (each one step further, sized at the average visible level) until circuit limit.
SYNTH_STEP_PCT = 0.0005      # price step between synthetic levels (0.05%)
SYNTH_LEVELS = 400           # max synthetic levels
SYNTH_MIN_QTY = 100          # min size of a synthetic level
# Liquidity eaten by paper traders is shared by everyone and heals with this half-life,
# so a big buy lifts the ask for the next trader too, then the book recovers.
HALF_LIFE = float(os.getenv("IMPACT_HALF_LIFE_SEC", "90"))
# Market orders won't walk the book further than this % away from LTP
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "10"))

# --- sessions ------------------------------------------------------------
MOCK = os.getenv("MOCK_FEED", "") == "1" or not os.getenv("ANGEL_API_KEY")
ENFORCE_HOURS = os.getenv("ENFORCE_MARKET_HOURS", "0" if MOCK else "1") == "1"
SQUAREOFF_HM = (15, 20)      # MIS auto square-off time (IST)
MAX_QTY = 10_000_000
