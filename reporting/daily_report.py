"""
Daily report generator.
Reads state.json + trades.log and saves a markdown report to logs/reports/YYYY-MM-DD.md.
Run automatically at EOD, or manually: py -3.12 reporting/daily_report.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

ET = pytz.timezone("America/New_York")
REPORTS_DIR = "logs/reports"


def generate(portfolio=None) -> str:
    """
    Build the daily markdown report.
    If portfolio is passed (live run), use it directly.
    Otherwise reads from state.json (manual/standalone run).
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

    # Load state
    if portfolio is not None:
        state = portfolio.snapshot()
    else:
        try:
            with open(config.STATE_FILE) as f:
                state = json.load(f)
        except Exception as e:
            return f"# Report Error\nCould not load state.json: {e}\n"

    account_value = state.get("account_value", config.ACCOUNT_SIZE)
    daily_pnl = state.get("daily_pnl", 0.0)
    trades = state.get("closed_today", [])
    scanner = state.get("scanner", [])

    # Per-strategy aggregation
    strat_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "conditions": defaultdict(int)})
    wins, losses = [], []

    for t in trades:
        pnl = t.get("pnl", 0.0)
        strat = t.get("strategy", "unknown")
        s = strat_stats[strat]
        s["trades"] += 1
        s["pnl"] += pnl
        if pnl >= 0:
            s["wins"] += 1
            wins.append(t)
        else:
            s["losses"] += 1
            losses.append(t)
        # Parse conditions from notes field
        notes = t.get("notes", "")
        for cond in notes.split(","):
            cond = cond.strip()
            if cond:
                s["conditions"][cond] += 1

    total_trades = len(trades)
    total_wins = len(wins)
    total_losses = len(losses)
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    pnl_sign = "+" if daily_pnl >= 0 else ""

    lines = []

    # Header
    lines += [
        f"# CashCow Daily Report — {today}",
        f"_Generated: {now_str}_",
        "",
        "---",
        "",
        "## Account Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Account Value | ${account_value:,.2f} |",
        f"| Net P&L Today | {pnl_sign}${daily_pnl:,.2f} |",
        f"| Total Trades | {total_trades} |",
        f"| Wins | {total_wins} |",
        f"| Losses | {total_losses} |",
        f"| Win Rate | {win_rate:.1f}% |",
        f"| Tickers Scanned | {len(scanner)} |",
        "",
        "---",
        "",
    ]

    def _fmt_time(ts: str) -> str:
        try:
            return datetime.fromisoformat(ts).astimezone(ET).strftime("%H:%M")
        except Exception:
            return ts or "-"

    def _duration(entry_ts: str, exit_ts: str) -> str:
        try:
            e = datetime.fromisoformat(entry_ts).astimezone(ET)
            x = datetime.fromisoformat(exit_ts).astimezone(ET)
            mins = int((x - e).total_seconds() / 60)
            return f"{mins}m"
        except Exception:
            return "-"

    # What went well
    lines += ["## What Went Well", ""]
    if wins:
        lines += ["| Ticker | Strategy | In | Out | Held | Entry $ | Exit $ | P&L | Reason | Conditions |",
                  "|--------|----------|----|-----|------|---------|--------|-----|--------|------------|"]
        for t in sorted(wins, key=lambda x: x.get("pnl", 0), reverse=True):
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            et = _fmt_time(t.get("entry_time", ""))
            xt = _fmt_time(t.get("exit_time", ""))
            dur = _duration(t.get("entry_time", ""), t.get("exit_time", ""))
            lines.append(
                f"| {t.get('ticker')} | {t.get('strategy')} | {et} | {xt} | {dur} "
                f"| ${entry:.2f} | ${exit_:.2f} | +${pnl:.2f} ({pnl_pct:+.1%}) "
                f"| {t.get('exit_reason')} | {t.get('notes', '-')} |"
            )
        lines.append("")
    else:
        lines += ["_No profitable trades today._", ""]

    lines += ["---", ""]

    # What went wrong
    lines += ["## What Went Wrong", ""]
    if losses:
        lines += ["| Ticker | Strategy | In | Out | Held | Entry $ | Exit $ | P&L | Reason | Conditions |",
                  "|--------|----------|----|-----|------|---------|--------|-----|--------|------------|"]
        for t in sorted(losses, key=lambda x: x.get("pnl", 0)):
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            et = _fmt_time(t.get("entry_time", ""))
            xt = _fmt_time(t.get("exit_time", ""))
            dur = _duration(t.get("entry_time", ""), t.get("exit_time", ""))
            lines.append(
                f"| {t.get('ticker')} | {t.get('strategy')} | {et} | {xt} | {dur} "
                f"| ${entry:.2f} | ${exit_:.2f} | ${pnl:.2f} ({pnl_pct:+.1%}) "
                f"| {t.get('exit_reason')} | {t.get('notes', '-')} |"
            )
        lines.append("")
    else:
        lines += ["_No losing trades today._", ""]

    lines += ["---", ""]

    # Strategy breakdown
    lines += ["## Strategy Breakdown", ""]
    if strat_stats:
        lines += ["| Strategy | Trades | Wins | Losses | Win Rate | Net P&L | Avg P&L |",
                  "|----------|--------|------|--------|----------|---------|---------|"]
        for strat, s in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
            avg = s["pnl"] / s["trades"] if s["trades"] > 0 else 0
            sign = "+" if s["pnl"] >= 0 else ""
            lines.append(
                f"| {strat} | {s['trades']} | {s['wins']} | {s['losses']} "
                f"| {wr:.0f}% | {sign}${s['pnl']:.2f} | {sign}${avg:.2f} |"
            )
        lines.append("")

        # Top conditions per strategy
        lines += ["### Conditions Seen Per Strategy", ""]
        for strat, s in strat_stats.items():
            if s["conditions"]:
                cond_str = ", ".join(
                    f"`{c}` x{n}" for c, n in sorted(s["conditions"].items(), key=lambda x: -x[1])
                )
                lines.append(f"**{strat}**: {cond_str}")
        lines.append("")
    else:
        lines += ["_No trades to report._", ""]

    lines += ["---", ""]

    # Scanner
    lines += [
        "## Scanner Results",
        "",
        f"Tickers in watchlist today ({len(scanner)}): "
        + (", ".join(f"`{t}`" for t in scanner) if scanner else "_none_"),
        "",
        "---",
        "",
        "_End of report. Save this file and feed to an LLM for pattern analysis once you have enough data._",
    ]

    return "\n".join(lines)


def save_report(portfolio=None) -> str:
    """Generate and save the report. Returns the file path."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    path = os.path.join(REPORTS_DIR, f"{today}.md")
    content = generate(portfolio)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


if __name__ == "__main__":
    path = save_report()
    print(f"Report saved: {path}")
