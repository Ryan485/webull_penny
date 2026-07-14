"""
Live dashboard — runs at http://localhost:8050
Reads from logs/state.json (written by the bot every 10 seconds).
Design copied from the Korean bot dashboard
(C:\\cashcow\\korean_tradingbot\\tradingbot\\dashboard.py) on owner request
2026-07-14: stat cards, flat BUY/SELL trades table, live log panel.
No charts (owner never used them — don't re-add).
"""
import json
import os
from datetime import datetime

import dash
import dash_bootstrap_components as dbc
import pytz
from dash import dcc, html, Input, Output, callback, ALL, ctx
from dash.exceptions import PreventUpdate
import config

ET = pytz.timezone("America/New_York")

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    title="CashCow Penny Bot",
    update_title=None,
)

REFRESH_SECS = 5
LOG_LINES = 120

# ── Layout ─────────────────────────────────────────────────────────────────────

app.layout = html.Div([
    dcc.Interval(id="refresh", interval=REFRESH_SECS * 1000, n_intervals=0),
    html.Div(id="sell-notification"),

    html.H1("CashCow Penny Bot — Paper Trading", className="section"),
    html.Div(id="header-sub", className="sub"),

    html.Div(id="stat-cards", className="cards"),

    html.Div(id="positions-block"),

    html.H1("Trades", className="section"),
    html.Div(id="trades-table"),

    html.H1("Log", className="section"),
    html.Div("loading...", id="logs"),
], style={"padding": "0"})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not os.path.exists(config.STATE_FILE):
        return {}
    try:
        with open(config.STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _tstr(iso: str, with_secs: bool = True) -> str:
    """ISO timestamp -> HH:MM:SS ET for display."""
    try:
        fmt = "%H:%M:%S" if with_secs else "%H:%M"
        return datetime.fromisoformat(iso).strftime(fmt)
    except Exception:
        return "--:--"


def _pstr(price) -> str:
    """Price for display: sub-$1 pennies need 4 decimals, else 2
    ($0.5085 -> $0.5056 showed as '$0.51 -> $0.51' with a -0.6% loss)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return "$?"
    return f"${p:.4f}" if p < 1 else f"${p:.2f}"


def _log_tail(n: int = LOG_LINES) -> list:
    """Last n lines of bot.log without reading the whole (multi-MB) file."""
    path = "logs/bot.log"
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 96_000))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        return lines[-n:]
    except Exception:
        return []


def _stat_card(label: str, value: str, cls: str = "", card_cls: str = ""):
    return html.Div([
        html.Div(label, className="label"),
        html.Div(value, className=f"value {cls}".strip()),
    ], className=f"card-kr {card_cls}".strip())


def _sign_cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


# ── Callbacks ──────────────────────────────────────────────────────────────────

@callback(
    Output("header-sub", "children"),
    Output("stat-cards", "children"),
    Output("positions-block", "children"),
    Output("trades-table", "children"),
    Output("logs", "children"),
    Input("refresh", "n_intervals"),
)
def update_all(n):
    state = _load_state()
    now_et = datetime.now(ET).strftime("%H:%M:%S")

    positions = state.get("positions", {})
    closed = state.get("closed_today", [])
    scanner = state.get("scanner", [])

    sub = html.Div([
        html.Span(f"auto-refreshes every {REFRESH_SECS}s · updated {now_et} ET"),
        html.Span(" · watchlist: " if scanner else "",
                  style={"marginRight": "2px"}),
        *[html.Span(t, className="watch-chip") for t in scanner[:15]],
    ])

    # ── Stat cards ──
    acct = state.get("account_value", config.ACCOUNT_SIZE)
    dpnl = state.get("daily_pnl", 0.0)
    unreal = sum(p.get("unrealized_pnl", 0) for p in positions.values())
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    win_rate = f"{100 * wins / len(closed):.0f}%" if closed else "—"
    halted = state.get("halt", False)

    cards = [
        _stat_card("Account ($)", f"{acct:,.0f}"),
        _stat_card("Daily P&L ($)", f"{dpnl:+,.0f}", _sign_cls(dpnl),
                   card_cls=f"card-{_sign_cls(dpnl)}"),
        _stat_card("Unrealized ($)", f"{unreal:+,.0f}", _sign_cls(unreal),
                   card_cls=f"card-{_sign_cls(unreal)}"),
        _stat_card("Buys", str(len(closed) + len(positions))),
        _stat_card("Sells", str(len(closed))),
        _stat_card("Win rate", win_rate),
        _stat_card("Status", "HALT" if halted else "ACTIVE",
                   "neg" if halted else "pos"),
    ]

    # ── Open positions: same card design, with the manual Sell Now button ──
    if positions:
        pos_cards = []
        for ticker, p in positions.items():
            pnl = p.get("unrealized_pnl", 0)
            pct = p.get("pnl_pct", 0)
            pos_cards.append(html.Div([
                html.Div(f"{ticker} · {p.get('strategy','?')} · "
                         f"in {_tstr(p.get('entry_time',''), with_secs=False)}",
                         className="label"),
                html.Div(f"{_pstr(p['entry_price'])} → "
                         f"{_pstr(p.get('current_price', p['entry_price']))}",
                         className="value"),
                html.Div(f"${pnl:+,.2f} ({pct:+.1%})",
                         className=f"value {_sign_cls(pnl)}",
                         style={"fontSize": "14px"}),
                html.Div(f"stop {_pstr(p['stop_price'])} · "
                         f"tgt {_pstr(p['target_price'])}",
                         className="label"),
                html.Button("Sell Now", className="sellbtn",
                            id={"type": "sell-btn", "ticker": ticker}),
            ], className=f"card-kr card-{_sign_cls(pnl)}"))
        pos_block = html.Div([
            html.H1("Open Positions", className="section"),
            html.Div(pos_cards, className="cards"),
        ])
    else:
        pos_block = html.Div()

    # ── Trades table: one row per fill (BUY and SELL), newest first ──
    events = []
    for t in closed:
        qty = t.get("shares", 0)
        events.append({
            "time": t.get("exit_time", ""), "side": "SELL",
            "ticker": t.get("ticker", "?"), "qty": qty,
            "price": t.get("exit_price", 0),
            "pct": t.get("pnl_pct", 0), "pnl": t.get("pnl", 0),
            "reason": f"{str(t.get('exit_reason','')).upper()} "
                      f"({t.get('pnl_pct',0):+.1%})",
        })
        events.append({
            "time": t.get("entry_time", ""), "side": "BUY",
            "ticker": t.get("ticker", "?"), "qty": qty,
            "price": t.get("entry_price", 0),
            "pct": None, "pnl": None,
            "reason": t.get("strategy", ""),
        })
    for ticker, p in positions.items():
        events.append({
            "time": p.get("entry_time", ""), "side": "BUY",
            "ticker": ticker, "qty": p.get("shares", 0),
            "price": p.get("entry_price", 0),
            "pct": None, "pnl": None,
            "reason": f"{p.get('strategy','')} — open",
        })
    events.sort(key=lambda e: e["time"], reverse=True)

    if events:
        body = []
        for e in events[:60]:
            if e["pnl"] is None:
                pct_td = html.Td("")
                pnl_td = html.Td("")
            else:
                cls = _sign_cls(e["pnl"])
                pct_td = html.Td(f"{e['pct']:+.2%}", className=cls)
                pnl_td = html.Td(f"{e['pnl']:+,.0f}", className=cls)
            body.append(html.Tr([
                html.Td(_tstr(e["time"])),
                html.Td(e["side"], className=f"side-{e['side']}"),
                html.Td(e["ticker"], className="ticker"),
                html.Td(f"{e['qty']:,}"),
                html.Td(_pstr(e["price"])),
                pct_td,
                pnl_td,
                html.Td(e["reason"]),
            ]))
        trades_div = html.Table([
            html.Thead(html.Tr([
                html.Th(h) for h in
                ["Time (ET)", "Side", "Ticker", "Qty", "Price",
                 "Profit %", "P&L ($)", "Reason"]
            ])),
            html.Tbody(body),
        ], className="trades")
    else:
        trades_div = html.Table([
            html.Tbody(html.Tr(html.Td("no trades yet"))),
        ], className="trades")

    # ── Log tail (bot.log), WARNING/ERROR highlighted like the reference ──
    log_items = []
    for line in _log_tail():
        cls = ("logline error" if "ERROR" in line
               else "logline warn" if "WARNING" in line
               else "logline")
        log_items.append(html.Div(line, className=cls))
    logs_div = log_items or ["no log yet"]

    return sub, cards, pos_block, trades_div, logs_div


@callback(
    Output("sell-notification", "children"),
    Input({"type": "sell-btn", "ticker": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def handle_sell_click(n_clicks):
    if not ctx.triggered_id or not any(n for n in n_clicks if n):
        raise PreventUpdate
    ticker = ctx.triggered_id["ticker"]
    try:
        from trading.manual_control import execute_sell
        ok, msg = execute_sell(ticker)
        return dbc.Alert(msg, color="success" if ok else "danger",
                         dismissable=True, duration=6000)
    except Exception as e:
        return dbc.Alert(f"Sell failed: {e}", color="danger", dismissable=True)


def run_dashboard():
    app.run(debug=False, host="0.0.0.0", port=config.DASHBOARD_PORT, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
