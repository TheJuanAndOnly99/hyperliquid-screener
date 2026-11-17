"""Terminal dashboard, Discord alerts, forward-test log and reports for the Hyperliquid screener."""
import argparse
import csv
import os
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

import config as cfg
import screener
from screener import Setup

LOG_FIELDS = ["logged_at", *(f.name for f in fields(Setup)), "status", "exit_price", "r_result", "closed_at"]


def px(value: float) -> str:
    return f"{value:,.6g}"


def exit_by(opened: datetime, timeframe: str) -> datetime:
    return opened + timedelta(milliseconds=cfg.MAX_HOLD_BARS * cfg.INTERVAL_MS[timeframe])


def ci_text(r: dict) -> str:
    return f"{r['ci_low']:+.2f} to {r['ci_high']:+.2f}" if r["trades"] >= 2 else "-"


def row_key(row: dict) -> str:
    return f"{row['coin']}:{row['timeframe']}:{row['side']}"


def stats_table(title: str, rows: list[dict]) -> Table:
    """Rows are dicts with group, trades, win_rate, avg_r, total_r."""
    table = Table(title=title, expand=True)
    for col in ("Group", "Trades", "Win rate", "Avg R", "95% CI of avg R", "Total R"):
        table.add_column(col, justify="left" if col == "Group" else "right")
    for r in rows:
        style = "green" if r["avg_r"] > 0 else "red" if r["trades"] else "dim"
        table.add_row(r["group"], str(r["trades"]), f"{r['win_rate']:.1%}" if r["trades"] else "-",
                      Text(f"{r['avg_r']:+.3f}", style=style), ci_text(r), Text(f"{r['total_r']:+.1f}", style=style))
    return table


# ---- forward-test log -------------------------------------------------------

def read_log() -> list[dict]:
    path = Path(cfg.SETUP_LOG_PATH)
    if not cfg.SETUP_LOG_PATH or not path.exists():
        return []
    with path.open(newline="") as f:
        return [{k: row.get(k) or "" for k in LOG_FIELDS} for row in csv.DictReader(f)]


def write_log(rows: list[dict]) -> None:
    tmp = Path(cfg.SETUP_LOG_PATH + ".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, cfg.SETUP_LOG_PATH)


def update_log(info) -> tuple[list[dict], list[str]]:
    """Resolves open logged setups (stop / TP1 / time exit). Returns (rows, errors)."""
    if not cfg.SETUP_LOG_PATH:
        return [], []
    rows, errors, changed = read_log(), [], False
    for row in rows:
        if row["status"] not in ("", "OPEN"):
            continue
        try:
            outcome = screener.resolve_outcome(
                info, row["coin"], row["timeframe"], row["side"], float(row["entry"]), float(row["stop"]),
                float(row["tp1"]), int(datetime.fromisoformat(row["logged_at"]).timestamp() * 1000))
        except Exception as exc:
            errors.append(f"resolve {row['coin']}: {str(exc)[:80]}")
            outcome = None
        row.update(outcome or {"status": "OPEN"})
        changed = changed or bool(outcome)
        time.sleep(cfg.REQUEST_SLEEP)
    if changed:
        write_log(rows)
    return rows, errors


def append_log(rows: list[dict], new: list[Setup], now: datetime) -> list[dict]:
    if cfg.SETUP_LOG_PATH and new:
        stamp = now.isoformat(timespec="seconds")
        rows = rows + [{**dict.fromkeys(LOG_FIELDS, ""), "logged_at": stamp, **asdict(s), "status": "OPEN"} for s in new]
        write_log(rows)
    return rows


def log_summary(rows: list[dict]) -> list[dict]:
    closed = [r for r in rows if r["r_result"] != ""]
    groups = [("All closed", closed)] + [(side, [r for r in closed if r["side"] == side]) for side in ("LONG", "SHORT")]
    return [{"group": name, **screener.trade_stats([float(r["r_result"]) for r in sub])} for name, sub in groups]


def build_report_view(rows: list[dict], errors: list[str]) -> Group:
    counts = {s: sum(r["status"] == s for r in rows) for s in ("OPEN", "TP1", "STOP", "TIMEOUT", "UNRESOLVED")}
    parts = [
        stats_table(f"Forward test: {cfg.SETUP_LOG_PATH} (net of taker fees, funding excluded)", log_summary(rows)),
        Text(" | ".join(f"{k}: {v}" for k, v in counts.items()) + " | compare with: python main.py --backtest", style="dim"),
    ]
    if errors:
        parts.append(Text("; ".join(errors[:3]), style="red"))
    return Group(*parts)


# ---- live dashboard ---------------------------------------------------------

def build_watch_table(watch: list[dict]) -> Table:
    table = Table(title="Watchlist: near setups (not signals)", expand=True, style="dim")
    for col in ("Coin", "TF", "Side", "Price", "Band", "Dist to band", "RSI", "ADX"):
        table.add_column(col, justify="left" if col in ("Coin", "TF", "Side") else "right")
    for w in watch:
        table.add_row(
            w["coin"], w["timeframe"], Text(w["side"], style="green" if w["side"] == "LONG" else "red"),
            px(w["price"]), px(w["band"]),
            "beyond band" if w["dist_pct"] <= 0 else f"{w['dist_pct']:.2f}%", f"{w['rsi']:.1f}",
            Text(f"{w['adx']:.0f}" + (" strong trend" if w["strong_trend"] else ""),
                 style="yellow" if w["strong_trend"] else ""),
        )
    return table


def build_view(setups: list[Setup], watch: list[dict], perps: list[dict], errors: list[str],
               seen: dict[str, datetime], log_rows: list[dict], live: bool = True) -> Group:
    table = Table(title="Hyperliquid Mean-Reversion Screener", expand=True)
    cols = ("Coin", "TF", "Side", "Entry", "Stop", "TP1 (SMA)", "TP2 (Band)", "R/R", "R/R TP2", "RSI",
            "Funding APR", "24h Vol", "Exit by")
    for col in cols:
        table.add_column(col, justify="left" if col in ("Coin", "TF", "Side") else "right")
    for s in setups:
        color = "green" if s.side == "LONG" else "red"
        funding = Text(f"{s.funding_apr:+.1f}% {'pays you' if s.funding_pays else 'costs you'}",
                       style="green" if s.funding_pays else "yellow")
        table.add_row(
            s.coin, s.timeframe, Text(s.side, style=f"bold {color}"),
            px(s.entry), px(s.stop), px(s.tp1), px(s.tp2),
            Text(f"{s.rr:.2f}", style="bold" if s.rr >= 1 else "dim"), f"{s.rr2:.2f}",
            f"{s.rsi:.1f}", funding, f"${s.volume_24h / 1e6:,.0f}M",
            f"{exit_by(seen.get(s.key, datetime.now()), s.timeframe).astimezone():%a %H:%M}",
        )
    if not setups:
        table.add_row(*["-"] * len(cols))

    status = (f"{datetime.now():%Y-%m-%d %H:%M:%S} | {len(setups)} setup(s) across {len(perps)} perps "
              f"on {'/'.join(cfg.TIMEFRAMES)} | {f'refresh every {cfg.REFRESH_SECONDS}s | ' if live else ''}"
              f"read-only, not financial advice")
    parts = [table, *([build_watch_table(watch)] if watch else []), Text(status, style="dim")]
    if log_rows:
        overall = log_summary(log_rows)[0]
        open_count = sum(r["status"] == "OPEN" for r in log_rows)
        result = f", {overall['win_rate']:.0%} wins, avg {overall['avg_r']:+.2f}R" if overall["trades"] else ""
        parts.append(Text(f"Forward test: {open_count} open, {overall['trades']} closed{result} "
                          f"(python main.py --report)", style="dim"))
    if errors:
        parts.append(Text(f"{len(errors)} error(s): " + "; ".join(errors[:3]), style="red"))
    return Group(*parts)


def build_backtest_view(grid: list[dict], breakdown: list[dict], errors: list[str]) -> Group:
    table = Table(title="Backtest: exit at stop, TP1 or timeout, net of taker fees (funding excluded)", expand=True)
    for col in ("TF", "History", "Trend filter", "Min R/R", "Trades", "Win rate", "Avg R", "95% CI of avg R", "Total R"):
        table.add_column(col, justify="left" if col in ("TF", "Trend filter") else "right")
    for r in grid:
        style = "green" if r["avg_r"] > 0 else "red"
        table.add_row(
            r["timeframe"], f"{r['days']:.0f}d", "on" if r["trend_filter"] else "off", f"{r['min_rr']:.2f}",
            str(r["trades"]), f"{r['win_rate']:.1%}",
            Text(f"{r['avg_r']:+.3f}", style=style), ci_text(r), Text(f"{r['total_r']:+.1f}", style=style),
        )
    current = f"trend filter {'on' if cfg.TREND_FILTER else 'off'}, min R/R {cfg.MIN_RR:.2f}"
    parts = [
        table,
        stats_table(f"Breakdown for the current config ({current}): by side, then by coin", breakdown),
        Text("The interval treats trades as independent; setups cluster in time, so the true range is wider. "
             "Universe is today's top perps by volume (survivorship bias); per-coin samples are small; "
             "past results do not predict future ones.", style="dim"),
    ]
    if errors:
        parts.append(Text(f"{len(errors)} fetch error(s): " + "; ".join(errors[:3]), style="red"))
    return Group(*parts)


def send_discord(setups: list[Setup], seen: dict[str, datetime]) -> None:
    if not cfg.DISCORD_WEBHOOK_URL or not setups:
        return
    lines = [
        f"**{s.side} {s.coin}** ({s.timeframe}) entry {px(s.entry)} | SL {px(s.stop)} | TP1 {px(s.tp1)} | "
        f"TP2 {px(s.tp2)} | R/R {s.rr:.2f} | RSI {s.rsi:.1f} | funding {s.funding_apr:+.1f}% "
        f"({'pays' if s.funding_pays else 'costs'}) | exit by {exit_by(seen[s.key], s.timeframe).astimezone():%a %H:%M %Z}"
        for s in setups
    ]
    try:
        requests.post(cfg.DISCORD_WEBHOOK_URL, json={"content": "\n".join(lines)[:1900]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        Console(stderr=True).print(f"[red]Discord alert failed: {exc}[/red]")


def run_scan(info, seen: dict[str, datetime], once: bool = False) -> Group:
    """One scan. `seen` maps setup key -> first-seen time.

    A setup is logged and alerted once: not again while it stays on screen, and not while the
    log still holds an open trade for it (so repeated --once runs do not duplicate it).
    """
    try:
        setups, watch, perps, errors = screener.scan(info)
    except Exception as exc:  # keep the loop alive through API outages
        if once:  # exit non-zero so a scheduled run shows as failed
            raise
        return Group(Text(f"{datetime.now():%H:%M:%S} scan failed: {str(exc)[:200]}", style="red"))
    now, log_rows = datetime.now().astimezone(), []  # timezone-aware, so the log is portable across machines
    try:
        log_rows, log_errors = update_log(info)
        errors += log_errors
    except (OSError, ValueError) as exc:
        errors.append(f"setup log: {exc}")
    open_since = {row_key(r): datetime.fromisoformat(r["logged_at"]) for r in log_rows if r["status"] == "OPEN"}
    new = [s for s in setups if s.key not in seen and s.key not in open_since]
    current = {s.key: open_since.get(s.key) or seen.get(s.key, now) for s in setups}
    seen.clear()
    seen.update(current)
    try:
        log_rows = append_log(log_rows, new, now)
    except OSError as exc:
        errors.append(f"setup log: {exc}")
    send_discord(new, seen)
    return build_view(setups, watch, perps, errors, seen, log_rows, live=not once)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Hyperliquid mean-reversion screener")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single scan and exit")
    mode.add_argument("--backtest", action="store_true", help="replay the rules over history and exit")
    mode.add_argument("--report", action="store_true", help="resolve logged setups, show forward-test results and exit")
    args = parser.parse_args()

    console = Console()
    info = screener.make_info()
    seen: dict[str, datetime] = {}

    if args.backtest:
        with console.status("Backtesting..."):
            view = build_backtest_view(*screener.backtest(info))
        console.print(view)
    elif args.report:
        with console.status("Resolving logged setups..."):
            view = build_report_view(*update_log(info))
        console.print(view)
    elif args.once:
        with console.status("Scanning..."):
            view = run_scan(info, seen, once=True)
        console.print(view)
    else:
        try:
            with Live(Text("Scanning..."), console=console, refresh_per_second=1) as live:
                while True:
                    live.update(run_scan(info, seen))
                    time.sleep(cfg.REFRESH_SECONDS)
        except KeyboardInterrupt:
            console.print("Stopped.")


if __name__ == "__main__":
    main()
