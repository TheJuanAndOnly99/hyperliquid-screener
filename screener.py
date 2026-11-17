"""Read-only Hyperliquid data fetch, indicators, mean-reversion trade plans and backtest."""
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config as cfg

INDICATOR_COLS = ("sma", "bb_upper", "bb_lower", "rsi", "atr", "swing_low", "swing_high", "adx", "plus_di", "minus_di")


@dataclass
class Setup:
    coin: str
    timeframe: str
    side: str  # "LONG" | "SHORT"
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float   # TP1 reward / risk
    rr2: float  # TP2 reward / risk
    rsi: float
    atr: float
    funding_apr: float  # annualized, percent
    funding_pays: bool  # True if funding pays the trade direction
    volume_24h: float

    @property
    def key(self) -> str:
        return f"{self.coin}:{self.timeframe}:{self.side}"


def make_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _retry(fn, *args):
    """Calls fn, retrying with exponential backoff (rate limits, network blips)."""
    for attempt in range(cfg.MAX_RETRIES + 1):
        try:
            return fn(*args)
        except Exception:
            if attempt == cfg.MAX_RETRIES:
                raise
            time.sleep(cfg.RETRY_BACKOFF * 2 ** attempt)


def top_perps(info: Info, n: int | None = None) -> list[dict]:
    """Top-n perps by 24h notional volume with mark price and hourly funding."""
    n = n or cfg.TOP_N
    meta, ctxs = _retry(info.meta_and_asset_ctxs)
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted") or ctx.get("markPx") is None:
            continue
        rows.append({
            "coin": asset["name"],
            "mark": float(ctx["markPx"]),
            "funding": float(ctx.get("funding") or 0.0),
            "volume_24h": float(ctx.get("dayNtlVlm") or 0.0),
        })
    return sorted(rows, key=lambda r: r["volume_24h"], reverse=True)[:n]


def fetch_candles(info: Info, coin: str, interval: str, limit: int | None = None,
                  closed_only: bool | None = None) -> pd.DataFrame:
    limit = limit or cfg.CANDLE_LIMIT
    closed_only = cfg.USE_CLOSED_CANDLE if closed_only is None else closed_only
    end = int(time.time() * 1000)
    start = end - limit * cfg.INTERVAL_MS[interval]
    cols = ["time", "close_time", "open", "high", "low", "close", "volume"]
    raw = _retry(info.candles_snapshot, coin, interval, start, end)
    if not raw:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(raw).rename(
        columns={"t": "time", "T": "close_time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )[cols]
    df[cols[2:]] = df[cols[2:]].astype(float)
    if closed_only:
        df = df[df["close_time"] <= end]
    return df.sort_values("time").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds Bollinger bands, RSI, ATR, swing levels and ADX/DI (Wilder smoothing)."""
    df = df.copy()
    close = df["close"]
    df["sma"] = close.rolling(cfg.BB_PERIOD).mean()
    std = close.rolling(cfg.BB_PERIOD).std(ddof=0)
    df["bb_upper"] = df["sma"] + cfg.BB_STD * std
    df["bb_lower"] = df["sma"] - cfg.BB_STD * std

    def wilder(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    delta = close.diff()
    gain, loss = wilder(delta.clip(lower=0), cfg.RSI_PERIOD), wilder(-delta.clip(upper=0), cfg.RSI_PERIOD)
    df["rsi"] = (100 - 100 / (1 + gain / loss)).where(loss > 0, 100.0).where(gain.notna())

    prev_close = close.shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    df["atr"] = wilder(tr, cfg.ATR_PERIOD)

    df["swing_low"] = df["low"].rolling(cfg.SWING_LOOKBACK).min()
    df["swing_high"] = df["high"].rolling(cfg.SWING_LOOKBACK).max()

    up, down = df["high"].diff(), -df["low"].diff()
    tr_adx = wilder(tr, cfg.ADX_PERIOD)
    df["plus_di"] = 100 * wilder(up.where((up > down) & (up > 0), 0.0), cfg.ADX_PERIOD) / tr_adx
    df["minus_di"] = 100 * wilder(down.where((down > up) & (down > 0), 0.0), cfg.ADX_PERIOD) / tr_adx
    dx = 100 * (df["plus_di"] - df["minus_di"]).abs() / (df["plus_di"] + df["minus_di"])
    df["adx"] = wilder(dx, cfg.ADX_PERIOD)
    return df


def funding_stats(hourly_rate: float, side: str) -> tuple[float, bool]:
    """Annualized funding in percent, and whether it pays the given side."""
    apr = hourly_rate * cfg.FUNDING_PERIODS_PER_YEAR * 100
    pays = hourly_rate < 0 if side == "LONG" else hourly_rate > 0
    return apr, pays


def build_plan(row, entry: float, min_rr: float | None = None, trend_filter: bool | None = None) -> dict | None:
    """Trade plan for one candle (dict or Series), or None. Shared by the live scan and the backtest."""
    min_rr = cfg.MIN_RR if min_rr is None else min_rr
    trend_filter = cfg.TREND_FILTER if trend_filter is None else trend_filter
    if any(pd.isna(row[c]) for c in INDICATOR_COLS):
        return None

    if row["close"] <= row["bb_lower"] and row["rsi"] < cfg.RSI_LONG_MAX:
        side, sign = "LONG", 1
        stop, tp2 = row["swing_low"] - cfg.ATR_SL_MULT * row["atr"], row["bb_upper"]
        against_trend = row["minus_di"] > row["plus_di"]
    elif row["close"] >= row["bb_upper"] and row["rsi"] > cfg.RSI_SHORT_MIN:
        side, sign = "SHORT", -1
        stop, tp2 = row["swing_high"] + cfg.ATR_SL_MULT * row["atr"], row["bb_lower"]
        against_trend = row["plus_di"] > row["minus_di"]
    else:
        return None
    if trend_filter and against_trend and row["adx"] > cfg.ADX_MAX:
        return None  # fading a strong trend

    tp1 = row["sma"]
    risk, reward = (entry - stop) * sign, (tp1 - entry) * sign
    if risk <= 0 or reward <= 0 or reward / risk < min_rr:
        return None
    return {"side": side, "sign": sign, "stop": float(stop), "tp1": float(tp1), "tp2": float(tp2),
            "risk": float(risk), "rr": float(reward / risk), "rr2": float((tp2 - entry) * sign / risk)}


def evaluate(perp: dict, timeframe: str, df: pd.DataFrame) -> Setup | None:
    """Returns a Setup if the latest candle triggers a mean-reversion signal."""
    if df.empty:
        return None
    last = df.iloc[-1]
    plan = build_plan(last, perp["mark"])
    if not plan:
        return None
    apr, pays = funding_stats(perp["funding"], plan["side"])
    return Setup(
        coin=perp["coin"], timeframe=timeframe, side=plan["side"],
        entry=perp["mark"], stop=plan["stop"], tp1=plan["tp1"], tp2=plan["tp2"],
        rr=plan["rr"], rr2=plan["rr2"], rsi=float(last["rsi"]), atr=float(last["atr"]),
        funding_apr=apr, funding_pays=pays, volume_24h=perp["volume_24h"],
    )


def near_setup(perp: dict, timeframe: str, df: pd.DataFrame) -> dict | None:
    """Watchlist row if price and RSI are close to a signal (or a signal was filtered out), else None."""
    if df.empty:
        return None
    last = df.iloc[-1]
    if any(pd.isna(last[c]) for c in INDICATOR_COLS):
        return None
    for side, sign, band, rsi_near in (
        ("LONG", 1, last["bb_lower"], last["rsi"] < cfg.RSI_LONG_MAX + cfg.WATCH_RSI_MARGIN),
        ("SHORT", -1, last["bb_upper"], last["rsi"] > cfg.RSI_SHORT_MIN - cfg.WATCH_RSI_MARGIN),
    ):
        dist_pct = (perp["mark"] - band) / band * 100 * sign  # > 0: band not reached yet
        if rsi_near and dist_pct <= cfg.WATCH_BAND_PCT:
            return {"coin": perp["coin"], "timeframe": timeframe, "side": side, "price": perp["mark"],
                    "band": float(band), "dist_pct": float(dist_pct), "rsi": float(last["rsi"]),
                    "adx": float(last["adx"]), "strong_trend": bool(last["adx"] > cfg.ADX_MAX)}
    return None


def scan(info: Info | None = None) -> tuple[list[Setup], list[dict], list[dict], list[str]]:
    """Scans the top perps on all timeframes.

    Returns (setups sorted by R/R desc, watchlist sorted by distance to band,
    the ranked perp universe, error strings).
    """
    info = info or make_info()
    perps = top_perps(info)
    setups, watch, errors = [], [], []
    for perp in perps:
        for tf in cfg.TIMEFRAMES:
            try:
                df = add_indicators(fetch_candles(info, perp["coin"], tf))
                setup = evaluate(perp, tf, df)
                if setup:
                    setups.append(setup)
                elif near := near_setup(perp, tf, df):
                    watch.append(near)
            except Exception as exc:  # one bad symbol must not kill the scan
                errors.append(f"{perp['coin']} {tf}: {str(exc)[:80]}")
            time.sleep(cfg.REQUEST_SLEEP)
    return (sorted(setups, key=lambda s: s.rr, reverse=True),
            sorted(watch, key=lambda w: w["dist_pct"]), perps, errors)


def simulate(df: pd.DataFrame, min_rr: float, trend_filter: bool) -> list[tuple[str, float]]:
    """(side, net R multiple) of each trade: enter at the signal candle's close, exit at stop, TP1 or timeout.

    One trade at a time; if a bar touches both stop and TP1 the stop is assumed to fill first.
    """
    rows, results, free = df.to_dict("records"), [], 0
    signal = ((df["close"] <= df["bb_lower"]) & (df["rsi"] < cfg.RSI_LONG_MAX)) | (
        (df["close"] >= df["bb_upper"]) & (df["rsi"] > cfg.RSI_SHORT_MIN))
    for i in df.index[signal]:
        if i < free or i >= len(rows) - 1:
            continue
        entry = rows[i]["close"]
        plan = build_plan(rows[i], entry, min_rr, trend_filter)
        if not plan:
            continue
        sign, stop, tp1 = plan["sign"], plan["stop"], plan["tp1"]
        end = min(i + cfg.MAX_HOLD_BARS, len(rows) - 1)
        exit_px, j = rows[end]["close"], end
        for j in range(i + 1, end + 1):
            adverse, favorable = (rows[j]["low"], rows[j]["high"]) if sign == 1 else (rows[j]["high"], rows[j]["low"])
            if (adverse - stop) * sign <= 0:
                exit_px = stop
                break
            if (favorable - tp1) * sign >= 0:
                exit_px = tp1
                break
        results.append((plan["side"], ((exit_px - entry) * sign - 2 * cfg.TAKER_FEE * entry) / plan["risk"]))
        free = j + 1
    return results


def trade_stats(rs: list[float]) -> dict:
    """Trade count, win rate, average/total R and a bootstrap 95% interval on the average R."""
    n = len(rs)
    ci_low = ci_high = 0.0
    if n >= 2:
        arr, rng = np.asarray(rs), np.random.default_rng(0)
        means = arr[rng.integers(0, n, size=(cfg.BOOTSTRAP_SAMPLES, n))].mean(axis=1)
        ci_low, ci_high = (float(v) for v in np.percentile(means, [2.5, 97.5]))
    return {"trades": n, "win_rate": sum(r > 0 for r in rs) / n if n else 0.0,
            "avg_r": sum(rs) / n if n else 0.0, "total_r": sum(rs), "ci_low": ci_low, "ci_high": ci_high}


def backtest(info: Info | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Replays the live rules over history.

    Returns (grid of timeframe x trend filter x min R/R, breakdown by side and coin
    for the current config, error strings).
    """
    info = info or make_info()
    data, errors = {tf: [] for tf in cfg.TIMEFRAMES}, []
    for perp in top_perps(info):
        for tf in cfg.TIMEFRAMES:
            try:
                data[tf].append((perp["coin"], add_indicators(
                    fetch_candles(info, perp["coin"], tf, cfg.BACKTEST_CANDLES, closed_only=True))))
            except Exception as exc:
                errors.append(f"{perp['coin']} {tf}: {str(exc)[:80]}")
            time.sleep(cfg.REQUEST_SLEEP)

    grid, breakdown = [], []
    for tf, frames in data.items():
        days = max((len(f) for _, f in frames), default=0) * cfg.INTERVAL_MS[tf] / 86_400_000
        for trend_filter in (False, True):
            for min_rr in cfg.BACKTEST_MIN_RR_GRID:
                rs = [r for _, f in frames for _, r in simulate(f, min_rr, trend_filter)]
                grid.append({"timeframe": tf, "days": days, "trend_filter": trend_filter, "min_rr": min_rr,
                             **trade_stats(rs)})
        trades = [(coin, side, r) for coin, f in frames for side, r in simulate(f, cfg.MIN_RR, cfg.TREND_FILTER)]
        breakdown.append({"group": f"{tf} ALL", **trade_stats([r for _, _, r in trades])})
        for side in ("LONG", "SHORT"):
            breakdown.append({"group": f"{tf} {side}", **trade_stats([r for _, s, r in trades if s == side])})
        by_coin = [{"group": f"{tf} {coin}", **trade_stats([r for c, _, r in trades if c == coin])} for coin, _ in frames]
        breakdown += sorted(by_coin, key=lambda b: b["total_r"], reverse=True)
    return grid, breakdown, errors


def resolve_outcome(info: Info, coin: str, timeframe: str, side: str, entry: float, stop: float,
                    tp1: float, opened_ms: int) -> dict | None:
    """Outcome of a logged setup from fine-grained candles since it opened, or None if still open.

    Same exit rules and fee model as the backtest: stop, TP1, or close after MAX_HOLD_BARS.
    """
    now = int(time.time() * 1000)
    deadline = opened_ms + cfg.MAX_HOLD_BARS * cfg.INTERVAL_MS[timeframe]
    step = cfg.INTERVAL_MS[cfg.RESOLVE_INTERVAL]
    df = fetch_candles(info, coin, cfg.RESOLVE_INTERVAL, min(5000, (now - opened_ms) // step + 2), closed_only=True)
    df = df[(df["time"] >= opened_ms) & (df["close_time"] <= deadline)]
    sign = 1 if side == "LONG" else -1

    status = exit_px = closed_ms = None
    for bar in df.itertuples():
        adverse, favorable = (bar.low, bar.high) if sign == 1 else (bar.high, bar.low)
        if (adverse - stop) * sign <= 0:
            status, exit_px = "STOP", stop
        elif (favorable - tp1) * sign >= 0:
            status, exit_px = "TP1", tp1
        if status:
            closed_ms = bar.close_time
            break
    if not status:
        if now < deadline:
            return None
        if df.empty:  # too old to fetch at this candle size
            return {"status": "UNRESOLVED", "exit_price": "", "r_result": "", "closed_at": ""}
        status, exit_px, closed_ms = "TIMEOUT", float(df.iloc[-1]["close"]), deadline
    r = ((exit_px - entry) * sign - 2 * cfg.TAKER_FEE * entry) / abs(entry - stop)
    return {"status": status, "exit_price": exit_px, "r_result": round(r, 4),
            "closed_at": datetime.fromtimestamp(closed_ms / 1000).astimezone().isoformat(timespec="seconds")}
