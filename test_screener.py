"""Offline checks for the indicator, trade-plan and backtest math (no network).

Run: python test_screener.py   (or: python -m pytest -q)
"""
import numpy as np
import pandas as pd

import screener as s

RANGE = 100 + np.sin(np.arange(80) / 3)
SELLOFF = np.r_[RANGE, np.linspace(100, 90, 8)]


def frame(closes) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    t = np.arange(len(c)) * 3_600_000
    return pd.DataFrame({"time": t, "close_time": t + 3_599_999, "open": c, "high": c + 0.3,
                         "low": c - 0.3, "close": c, "volume": 1.0})


def last_row(closes) -> dict:
    return s.add_indicators(frame(closes)).iloc[-1].to_dict()


def test_indicators_are_sane():
    row = last_row(SELLOFF)
    assert row["bb_lower"] < row["sma"] < row["bb_upper"]
    assert 0 <= row["rsi"] <= 100 and row["atr"] > 0 and 0 <= row["adx"] <= 100
    assert row["swing_low"] <= row["low"] and row["swing_high"] >= row["high"]


def test_long_plan_levels_and_rr():
    row = last_row(SELLOFF)
    plan = s.build_plan(row, row["close"], min_rr=0.0, trend_filter=False)
    assert plan["side"] == "LONG"
    assert plan["stop"] < row["close"] < plan["tp1"] < plan["tp2"]
    assert abs(plan["rr"] - (plan["tp1"] - row["close"]) / (row["close"] - plan["stop"])) < 1e-9


def test_short_plan_mirrors_long():
    row = last_row(200 - SELLOFF)
    plan = s.build_plan(row, row["close"], min_rr=0.0, trend_filter=False)
    assert plan["side"] == "SHORT"
    assert plan["tp2"] < plan["tp1"] < row["close"] < plan["stop"]


def test_no_plan_inside_the_bands():
    row = last_row(RANGE)
    assert s.build_plan(row, row["close"], min_rr=0.0, trend_filter=False) is None


def test_trend_filter_blocks_fading_a_strong_trend():
    row = {**last_row(SELLOFF), "adx": 45.0, "minus_di": 35.0, "plus_di": 10.0}
    assert s.build_plan(row, row["close"], min_rr=0.0, trend_filter=True) is None
    assert s.build_plan({**row, "adx": 15.0}, row["close"], min_rr=0.0, trend_filter=True) is not None


def test_min_rr_filter():
    row = last_row(SELLOFF)
    rr = s.build_plan(row, row["close"], min_rr=0.0, trend_filter=False)["rr"]
    assert s.build_plan(row, row["close"], min_rr=rr + 0.01, trend_filter=False) is None


def test_funding_alignment():
    apr, pays = s.funding_stats(-0.00001, "LONG")
    assert pays and abs(apr - (-8.76)) < 1e-9
    assert s.funding_stats(0.00001, "SHORT")[1] and not s.funding_stats(0.00001, "LONG")[1]


def test_simulate_wins_on_reversion_and_loses_on_continuation():
    dip_and_recover = s.simulate(s.add_indicators(frame(np.r_[RANGE, [97.5, 96, 97, 98.5, 100, 101]])), 0.0, False)
    crash = s.simulate(s.add_indicators(frame(np.r_[SELLOFF, np.linspace(89, 60, 30)])), 0.0, False)
    assert len(dip_and_recover) == 1 and dip_and_recover[0][0] == "LONG" and dip_and_recover[0][1] > 0
    assert crash and all(side == "LONG" and -1.2 < r < -1.0 for side, r in crash)  # full stop plus fees


def test_trade_stats():
    stats = s.trade_stats([1.0, -1.0, 1.0, 1.0])
    assert stats["trades"] == 4 and stats["win_rate"] == 0.75 and stats["avg_r"] == 0.5
    assert stats["ci_low"] <= stats["avg_r"] <= stats["ci_high"]
    assert s.trade_stats([])["trades"] == 0


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
    print(f"{len(tests)} tests passed")
