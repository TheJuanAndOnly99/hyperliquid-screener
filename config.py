"""Settings for the read-only Hyperliquid mean-reversion screener."""
import os

# Universe / data
TOP_N = 20
TIMEFRAMES = ("1h",)  # 4h dropped: no variant survived out-of-sample; add "4h" back to scan it
CANDLE_LIMIT = 100
REQUEST_SLEEP = 0.2  # seconds between candle requests
MAX_RETRIES = 3      # per API call, with exponential backoff
RETRY_BACKOFF = 1.0  # seconds; doubles on each retry
INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}
RESOLVE_INTERVAL = "5m"  # candle size used to resolve logged setups (stop / TP1 / time exit)

# Indicators
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_LONG_MAX = 25.0   # long when RSI < this (original spec: 32; 25/75 backtested better)
RSI_SHORT_MIN = 75.0  # short when RSI > this (original spec: 68)
ATR_PERIOD = 14
USE_CLOSED_CANDLE = True  # signal on the last closed candle, not the forming one

# Trend filter: skip setups that fade a strong trend (ADX above max, DI against the trade)
TREND_FILTER = True
ADX_PERIOD = 14
ADX_MAX = 30.0

# Trade plan
ATR_SL_MULT = 1.5
SWING_LOOKBACK = 10  # candles used for the recent swing low/high
MIN_RR = 1.0         # drop setups whose TP1 R/R is below this (least-bad value in the backtest)

# Watchlist: coins close to triggering
WATCH_RSI_MARGIN = 8.0  # RSI within this many points of the threshold
WATCH_BAND_PCT = 0.5    # price within this % of the band (or already beyond it)

# Forward-test log: each new setup is appended here (empty = off)
SETUP_LOG_PATH = "setups_log.csv"

# Backtest (python main.py --backtest)
BACKTEST_CANDLES = 5000            # per symbol and timeframe (API max 5000)
BOOTSTRAP_SAMPLES = 2000           # resamples for the 95% interval on average R
BACKTEST_MIN_RR_GRID = (0.0, 0.5, 0.75, 1.0)
MAX_HOLD_BARS = 24                 # exit at close if neither stop nor TP1 is hit
TAKER_FEE = 0.00045                # per side

# Funding (Hyperliquid funding is paid hourly)
FUNDING_PERIODS_PER_YEAR = 24 * 365

# Runtime
REFRESH_SECONDS = 300
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")  # empty = alerts off
