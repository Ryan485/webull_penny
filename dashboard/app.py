"""
Live dashboard — runs at http://localhost:8050
Reads from logs/state.json (written by the bot every 10 seconds).
Layout (redesigned 2026-07-14 to match the owner's Korean-bot reference):
stat cards, open positions with manual Sell Now, and a flat BUY/SELL
trades table. The candlestick chart was removed on request (unused).
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

GREEN = "#00bc8c"
RED = "#e74c3c"
BLUE = "#61afef"
MUTED = "#adb5bd"

CARD_STYLE = {
    "backgroundColor": "#161a1e",
    "border": "1px solid #2a2f34",
    "borderRadius": "8px",
}

# ── Layout ─────────────────────────────────────────────────────────────────────

app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Interval(id="refresh", interval=REFRESH_SECS * 1000, n_intervals=0),
        html.Div(id="sell-notification", className="text-center mb-1"),

        html.Div([
            html.H3("CashCow Penny Bot — Paper Trading",
                    className="mt-3 mb-0", style={"fontWeight": "700"}),
            html.Div(id="header-sub", className="text-muted small mb-3"),
        ]),

        dbc.Row(id="stat-cards", className="g-2 mb-3"),

        html.H5("Open Positions", className="text-info"),
        html.Div(id="positions-table", className="mb-3"),

        html.H5("Trades", className="text-info"),
        html.Div(id="trades-table", className="mb-3"),

        dbc.Row([
            dbc.Col([
                html.H6("Today's Viral Stocks", className="text-info"),
                html.Div(id="scanner-list"),
            ], width=4),
            dbc.Col([
                html.H6("Recent Signals", className="text-info"),
                html.Div(id="signals-log",
                         style={"maxHeight": "220px", "overflowY": "auto",
                                "fontSize": "12px"}),
            ], width=8),
        ], className="mb-4"),
    ]
)


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


def _stat_card(label: str, value: str, color: str = "#ffffff"):
    return dbc.Col(
        dbc.Card(dbc.CardBody([
            html.Div(label, className="text-muted",
                     style={"fontSize": "12px"}),
            html.Div(value, style={"fontSize": "24px", "fontWeight": "700",
                                   "color": color}),
        ], className="p-2 px-3"), style=CARD_STYLE),
        width="auto",
    )


_TH_STYLE = {"color": MUTED, "fontSize": "12px", "fontWeight": "600",
             "textTransform": "uppercase", "borderBottom": "1px solid #2a2f34",
             "padding": "6px 14px", "textAlign": "left"}
_TD_STYLE = {"fontSize": "14px", "padding": "8px 14px",
             "borderBottom": "1px solid #1d2125", "whiteSpace": "nowrap"}


def _cell(content, color=None, bold=False, extra=None):
    style = dict(_TD_STYLE)
    if color:
        style["color"] = color
    if bold:
        style["fontWeight"] = "700"
    if extra:
        style.update(extra)
    return html.Td(content, style=style)


# ── Callbacks ──────────────────────────────────────────────────────────────────

@callback(
    Output("header-sub", "children"),
    Output("stat-cards", "children"),
    Output("positions-table", "children"),
    Output("trades-table", "children"),
    Output("scanner-list", "children"),
    Output("signals-log", "children"),
    Input("refresh", "n_intervals"),
)
def update_all(n):
    state = _load_state()
    now_et = datetime.now(ET).strftime("%H:%M:%S")
    sub = f"auto-refreshes every {REFRESH_SECS}s · updated {now_et} ET"

    positions = state.get("positions", {})
    closed = state.get("closed_today", [])

    # ── Stat cards ──
    acct = state.get("account_value", config.ACCOUNT_SIZE)
    dpnl = state.get("daily_pnl", 0.0)
    unreal = sum(p.get("unrealized_pnl", 0) for p in positions.values())
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    losses = len(closed) - wins
    win_rate = f"{wins / len(closed):.0%}" if closed else "—"
    buys = len(closed) + len(positions)
    halted = state.get("halt", False)

    cards = [
        _stat_card("Account ($)", f"{acct:,.0f}"),
        _stat_card("Daily P&L ($)", f"{dpnl:+,.0f}",
                   GREEN if dpnl >= 0 else RED),
        _stat_card("Unrealized ($)", f"{unreal:+,.0f}",
                   GREEN if unreal >= 0 else RED),
        _stat_card("Buys", str(buys)),
        _stat_card("Sells", str(len(closed))),
        _stat_card("Win rate", f"{win_rate}" + (f" ({wins}W/{losses}L)" if closed else "")),
        _stat_card("Status", "HALT" if halted else "ACTIVE",
                   RED if halted else GREEN),
    ]

    # ── Open positions (cards kept — they carry the manual Sell Now button) ──
    if positions:
        rows = []
        for ticker, p in positions.items():
            pnl = p.get("unrealized_pnl", 0)
            pct = p.get("pnl_pct", 0)
            rows.append(dbc.Col(dbc.Card(
                dbc.CardBody([
                    html.B(ticker, style={"color": "#f0a500", "fontSize": "15px"}),
                    html.Span(f" [{p.get('strategy','?')}]",
                              style={"color": MUTED, "fontSize": "11px"}),
                    html.Span(f"  bought {_tstr(p.get('entry_time',''), with_secs=False)} ET",
                              style={"color": MUTED, "fontSize": "11px"}),
                    html.Br(),
                    html.Span(f"Entry: {_pstr(p['entry_price'])}  ",
                              style={"color": "#e0e0e0", "fontSize": "12px"}),
                    html.Span(f"Now: {_pstr(p.get('current_price', p['entry_price']))}",
                              style={"color": "#ffffff", "fontSize": "12px",
                                     "fontWeight": "bold"}),
                    html.Br(),
                    html.Span(
                        f"P&L: ${pnl:+.2f} ({pct:+.1%})",
                        style={"color": GREEN if pnl >= 0 else RED,
                               "fontSize": "13px", "fontWeight": "bold"}
                    ),
                    html.Br(),
                    html.Span(
                        f"Stop: {_pstr(p['stop_price'])}   Target: {_pstr(p['target_price'])}",
                        style={"color": "#ced4da", "fontSize": "11px"}
                    ),
                    html.Br(),
                    dbc.Button(
                        "Sell Now",
                        id={"type": "sell-btn", "ticker": ticker},
                        color="danger", size="sm", className="mt-1",
                        style={"fontSize": "11px", "padding": "2px 10px"},
                    ),
                ], className="p-2"),
                style={"backgroundColor": "#0d2137",
                       "border": f"1px solid {GREEN}",
                       "borderRadius": "6px"},
            ), width="auto"))
        pos_div = dbc.Row(rows, className="g-2")
    else:
        pos_div = html.P("No open positions", className="text-muted small")

    # ── Trades table: one row per fill (BUY and SELL), newest first ──
    events = []
    for t in closed:
        qty = t.get("shares", 0)
        events.append({
            "time": t.get("exit_time", ""),
            "side": "SELL",
            "ticker": t.get("ticker", "?"),
            "qty": qty,
            "price": t.get("exit_price", 0),
            "pct": t.get("pnl_pct", 0),
            "pnl": t.get("pnl", 0),
            "reason": f"{str(t.get('exit_reason','')).upper()} ({t.get('pnl_pct',0):+.1%})",
        })
        events.append({
            "time": t.get("entry_time", ""),
            "side": "BUY",
            "ticker": t.get("ticker", "?"),
            "qty": qty,
            "price": t.get("entry_price", 0),
            "pct": None,
            "pnl": None,
            "reason": t.get("strategy", ""),
        })
    for ticker, p in positions.items():
        events.append({
            "time": p.get("entry_time", ""),
            "side": "BUY",
            "ticker": ticker,
            "qty": p.get("shares", 0),
            "price": p.get("entry_price", 0),
            "pct": None,
            "pnl": None,
            "reason": f"{p.get('strategy','')} — open",
        })
    events.sort(key=lambda e: e["time"], reverse=True)

    if events:
        body = []
        for e in events[:60]:
            side_color = RED if e["side"] == "SELL" else BLUE
            if e["pnl"] is None:
                pct_cell = _cell("")
                pnl_cell = _cell("")
            else:
                c = GREEN if e["pnl"] >= 0 else RED
                pct_cell = _cell(f"{e['pct']:+.2%}", color=c)
                pnl_cell = _cell(f"{e['pnl']:+,.0f}", color=c)
            body.append(html.Tr([
                _cell(_tstr(e["time"]), color=MUTED),
                _cell(e["side"], color=side_color, bold=True),
                _cell(e["ticker"], color="#f0a500", bold=True),
                _cell(f"{e['qty']:,}"),
                _cell(_pstr(e["price"])),
                pct_cell,
                pnl_cell,
                _cell(e["reason"], color=MUTED),
            ]))
        trades_div = html.Table([
            html.Thead(html.Tr([
                html.Th(h, style=_TH_STYLE)
                for h in ["Time (ET)", "Side", "Ticker", "Qty", "Price",
                          "Profit %", "P&L ($)", "Reason"]
            ])),
            html.Tbody(body),
        ], style={"width": "100%", "borderCollapse": "collapse"})
    else:
        trades_div = html.P("No trades today", className="text-muted small")

    # ── Scanner ──
    scanner = state.get("scanner", [])
    scan_div = html.Div(
        [dbc.Badge(t, color="warning", className="me-1 mb-1",
                   style={"fontSize": "12px"}) for t in scanner[:30]]
    ) if scanner else html.P("Scanning...", className="text-muted small")

    # ── Signals ──
    signals = state.get("signals", [])
    sig_items = []
    for s in reversed(signals[-30:]):
        color = "text-success" if s.get("score", 0) >= config.SCORE_THRESHOLD else "text-muted"
        notes = s.get("notes", "")
        if len(notes) > 90:
            notes = notes[:87] + "..."
        sig_items.append(
            html.Div(
                f"[{s.get('time','')}] {s.get('ticker','')} {s.get('strategy','')} "
                f"score={s.get('score',0)}/5 {notes}",
                className=f"small {color}"
            )
        )
    sigs_div = html.Div(sig_items or [html.P("No signals yet", className="text-muted small")])

    return sub, cards, pos_div, trades_div, scan_div, sigs_div


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
        return dbc.Alert(msg, color="success" if ok else "danger", dismissable=True, duration=6000)
    except Exception as e:
        return dbc.Alert(f"Sell failed: {e}", color="danger", dismissable=True)


def run_dashboard():
    app.run(debug=False, host="0.0.0.0", port=config.DASHBOARD_PORT, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
