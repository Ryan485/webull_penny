"""
Live dashboard — runs at http://localhost:8050
Reads from logs/state.json (written by the bot every 10 seconds).
Shows: account summary, open positions, scanner results, trade log, chart.
"""
import json
import os
from datetime import datetime

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import pytz
from plotly.subplots import make_subplots
from dash import dcc, html, Input, Output, State, callback, ALL, ctx
from dash.exceptions import PreventUpdate
import config

ET = pytz.timezone("America/New_York")

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    title="CashCow Penny Bot",
    update_title=None,
)

# ── Layout ─────────────────────────────────────────────────────────────────────

app.layout = dbc.Container(
    fluid=True,
    children=[
        dcc.Interval(id="refresh", interval=10_000, n_intervals=0),
        dcc.Interval(id="chart-refresh", interval=30_000, n_intervals=0),
        html.Div(id="sell-notification", className="text-center mb-1"),

        # Header
        dbc.Row([
            dbc.Col(html.H3("💰 CashCow Penny Bot", className="text-warning mt-3"), width=4),
            dbc.Col(html.Div(id="header-stats", className="mt-3 text-end"), width=8),
        ], className="mb-2"),

        dbc.Row([
            # Left column: positions + scanner
            dbc.Col([
                html.H5("Open Positions", className="text-info"),
                html.Div(id="positions-table"),
                html.Hr(),
                html.H5("Today's Viral Stocks", className="text-info mt-2"),
                html.Div(id="scanner-list"),
            ], width=4),

            # Center: chart
            dbc.Col([
                dbc.Row([
                    dbc.Col(
                        dcc.Dropdown(id="chart-ticker", placeholder="Select ticker to chart...",
                                     style={"backgroundColor": "#222"}),
                        width=8
                    ),
                    dbc.Col(
                        dbc.Button("Refresh Chart", id="chart-btn", color="secondary", size="sm"),
                        width=4, className="text-end"
                    ),
                ]),
                dcc.Graph(id="price-chart", style={"height": "520px"},
                          config={"displayModeBar": False}),
            ], width=5),

            # Right column: signals + trade log
            dbc.Col([
                html.H5("Recent Signals", className="text-info"),
                html.Div(id="signals-log", style={"height": "200px", "overflowY": "auto",
                                                   "fontSize": "12px"}),
                html.Hr(),
                html.H5("Trade Log (Today)", className="text-info mt-2"),
                html.Div(id="trade-log", style={"height": "200px", "overflowY": "auto",
                                                 "fontSize": "12px"}),
            ], width=3),
        ]),

        dbc.Row([
            dbc.Col([
                html.H5("Closed Trades Today", className="text-info mt-3"),
                html.Div(id="closed-trades"),
            ])
        ])
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


def _pnl_color(val: float) -> str:
    return "text-success" if val >= 0 else "text-danger"


def _tstr(iso: str) -> str:
    """ISO timestamp -> HH:MM ET for display."""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
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


# ── Callbacks ──────────────────────────────────────────────────────────────────

@callback(
    Output("header-stats", "children"),
    Output("positions-table", "children"),
    Output("scanner-list", "children"),
    Output("signals-log", "children"),
    Output("trade-log", "children"),
    Output("closed-trades", "children"),
    Output("chart-ticker", "options"),
    Input("refresh", "n_intervals"),
)
def update_all(n):
    state = _load_state()
    now_et = datetime.now(ET).strftime("%H:%M:%S ET")

    # Header
    acct = state.get("account_value", config.ACCOUNT_SIZE)
    dpnl = state.get("daily_pnl", 0.0)
    halted = state.get("halt", False)
    halt_badge = dbc.Badge("HALT", color="danger") if halted else dbc.Badge("ACTIVE", color="success")
    closed_stats = state.get("closed_today", [])
    wins = sum(1 for t in closed_stats if t.get("pnl", 0) > 0)
    losses = sum(1 for t in closed_stats if t.get("pnl", 0) <= 0)
    win_rate = f"{wins / len(closed_stats):.0%}" if closed_stats else "—"
    header = html.Div([
        dbc.Badge(f"Account: ${acct:,.2f}", color="secondary", className="me-2"),
        dbc.Badge(
            f"Daily P&L: ${dpnl:+,.2f}",
            color="success" if dpnl >= 0 else "danger",
            className="me-2"
        ),
        dbc.Badge(f"Trades: {len(closed_stats)} ({wins}W/{losses}L, {win_rate})",
                  color="info", className="me-2"),
        halt_badge,
        html.Span(f"  {now_et}", className="text-muted ms-3 small"),
    ])

    # Positions
    positions = state.get("positions", {})
    if positions:
        rows = []
        for ticker, p in positions.items():
            pnl = p.get("unrealized_pnl", 0)
            pct = p.get("pnl_pct", 0)
            rows.append(dbc.Card(
                dbc.CardBody([
                    html.B(ticker, style={"color": "#f0a500", "fontSize": "15px"}),
                    html.Span(f" [{p.get('strategy','?')}]",
                              style={"color": "#adb5bd", "fontSize": "11px"}),
                    html.Span(f"  bought {_tstr(p.get('entry_time',''))} ET",
                              style={"color": "#adb5bd", "fontSize": "11px"}),
                    html.Br(),
                    html.Span(f"Entry: {_pstr(p['entry_price'])}  ",
                              style={"color": "#e0e0e0", "fontSize": "12px"}),
                    html.Span(f"Now: {_pstr(p.get('current_price', p['entry_price']))}",
                              style={"color": "#ffffff", "fontSize": "12px",
                                     "fontWeight": "bold"}),
                    html.Br(),
                    html.Span(
                        f"P&L: ${pnl:+.2f} ({pct:+.1%})",
                        style={"color": "#00bc8c" if pnl >= 0 else "#e74c3c",
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
                className="mb-1",
                style={"backgroundColor": "#0d2137",
                       "border": "1px solid #00bc8c",
                       "borderRadius": "6px"},
            ))
        pos_div = html.Div(rows)
    else:
        pos_div = html.P("No open positions", className="text-muted small")

    # Scanner — clickable: loads the ticker into the chart
    scanner = state.get("scanner", [])
    scan_div = html.Div(
        [dbc.Button(
            t, id={"type": "scan-chart", "ticker": t},
            color="warning", outline=True, size="sm",
            className="me-1 mb-1", style={"fontSize": "12px", "padding": "2px 8px"},
        ) for t in scanner[:30]]
    ) if scanner else html.P("Scanning...", className="text-muted small")

    # Signals
    signals = state.get("signals", [])
    sig_items = []
    for s in reversed(signals[-30:]):
        color = "text-success" if s.get("score", 0) >= config.SCORE_THRESHOLD else "text-muted"
        notes = s.get("notes", "")
        if len(notes) > 70:
            notes = notes[:67] + "..."
        sig_items.append(
            html.Div(
                f"[{s.get('time','')}] {s.get('ticker','')} {s.get('strategy','')} "
                f"score={s.get('score',0)}/5 {notes}",
                className=f"small {color}"
            )
        )
    sigs_div = html.Div(sig_items or [html.P("No signals yet", className="text-muted small")])

    # Trade log from file
    log_lines = []
    if os.path.exists(config.LOG_FILE):
        try:
            with open(config.LOG_FILE) as f:
                lines = f.readlines()
            today = datetime.now(ET).strftime("%Y-%m-%d")
            log_lines = [l.strip() for l in lines if today in l][-30:]
        except Exception:
            pass
    log_div = html.Div(
        [html.Div(l, className="small text-muted") for l in reversed(log_lines)]
        or [html.P("No trades today", className="text-muted small")]
    )

    # Closed trades
    closed = state.get("closed_today", [])
    if closed:
        rows = []
        for t in reversed(closed[-20:]):
            pnl = t.get("pnl", 0)
            rows.append(dbc.Col(
                dbc.Card(dbc.CardBody([
                    html.B(t.get("ticker"),
                           style={"color": "#f0a500", "fontSize": "14px"}),
                    html.Span(f" {t.get('strategy')}",
                              style={"color": "#adb5bd", "fontSize": "11px"}),
                    html.Br(),
                    html.Span(
                        f"{_tstr(t.get('entry_time',''))} → "
                        f"{_tstr(t.get('exit_time',''))} ET",
                        style={"color": "#adb5bd", "fontSize": "11px"}
                    ),
                    html.Br(),
                    html.Span(
                        f"{_pstr(t.get('entry_price',0))} → {_pstr(t.get('exit_price',0))}  "
                        f"P&L: ${pnl:+.2f} ({t.get('pnl_pct',0):+.1%})  [{t.get('exit_reason')}]",
                        style={"color": "#00bc8c" if pnl >= 0 else "#e74c3c",
                               "fontSize": "12px"}
                    ),
                ], className="p-2"),
                style={"backgroundColor": "#0d2137",
                       "border": f"1px solid {'#00bc8c' if pnl >= 0 else '#e74c3c'}"}),
                width="auto",
            ))
        closed_div = dbc.Row(rows, className="g-1")
    else:
        closed_div = html.P("No closed trades today", className="text-muted small")

    # Dropdown options: watchlist + current positions
    all_tickers = list(positions.keys()) + scanner[:20]
    options = [{"label": t, "value": t} for t in dict.fromkeys(all_tickers)]

    return header, pos_div, scan_div, sigs_div, log_div, closed_div, options


@callback(
    Output("chart-ticker", "value"),
    Input({"type": "scan-chart", "ticker": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def chart_from_scanner(n_clicks):
    if not ctx.triggered_id or not any(n for n in n_clicks if n):
        raise PreventUpdate
    return ctx.triggered_id["ticker"]


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


def _empty_chart(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#111", plot_bgcolor="#111",
        annotations=[{"text": msg, "showarrow": False, "font": {"color": "gray", "size": 16}}]
    )
    return fig


@callback(
    Output("price-chart", "figure"),
    Input("chart-ticker", "value"),
    Input("chart-btn", "n_clicks"),
    Input("chart-refresh", "n_intervals"),
)
def update_chart(ticker, _clicks, _n):
    if not ticker:
        return _empty_chart("Select a ticker to chart")

    try:
        from data.market_data import get_chart_bars
        df, source = get_chart_bars(ticker)

        if df is None or df.empty:
            return _empty_chart(f"No data available for {ticker}")

        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                 "close": "Close", "volume": "Volume"})

        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.75, 0.25], vertical_spacing=0.03,
        )

        fig.add_trace(go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
            name=ticker,
            increasing_line_color="#00bc8c",
            decreasing_line_color="#e74c3c",
        ), row=1, col=1)

        # Moving averages (same ones the strategies use)
        ma5 = df["Close"].rolling(5).mean()
        ma10 = df["Close"].rolling(10).mean()
        fig.add_trace(go.Scatter(
            x=df.index, y=ma5, name="MA5",
            line={"color": "#c678dd", "width": 1},
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=ma10, name="MA10",
            line={"color": "#61afef", "width": 1},
        ), row=1, col=1)

        # VWAP overlay
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()
        fig.add_trace(go.Scatter(
            x=df.index, y=vwap, name="VWAP",
            line={"color": "#f39c12", "width": 1, "dash": "dash"},
        ), row=1, col=1)

        # Volume bars colored by candle direction
        vol_colors = [
            "#00bc8c" if c >= o else "#e74c3c"
            for o, c in zip(df["Open"], df["Close"])
        ]
        fig.add_trace(go.Bar(
            x=df.index, y=df["Volume"], name="Volume",
            marker_color=vol_colors, showlegend=False,
        ), row=2, col=1)

        # Stop / target / entry lines if this ticker is an open position
        state = _load_state()
        positions = state.get("positions", {})
        if ticker in positions:
            p = positions[ticker]
            fig.add_hline(y=p["stop_price"], line_color="#e74c3c",
                          line_dash="dash", annotation_text="Stop", row=1, col=1)
            fig.add_hline(y=p["target_price"], line_color="#00bc8c",
                          line_dash="dash", annotation_text="Target", row=1, col=1)
            fig.add_hline(y=p["entry_price"], line_color="#f0a500",
                          line_dash="dot", annotation_text="Entry", row=1, col=1)

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#111",
            plot_bgcolor="#111",
            title=f"{ticker} — 1min ({source})",
            xaxis_rangeslider_visible=False,
            margin={"l": 40, "r": 20, "t": 40, "b": 20},
            legend={"orientation": "h", "y": 1.08},
            # Preserve user zoom/pan across auto-refreshes
            uirevision=ticker,
        )
        # Hide overnight and weekend gaps so intraday candles fill the chart
        fig.update_xaxes(rangebreaks=[
            {"bounds": ["sat", "mon"]},
            {"bounds": [16, 9.5], "pattern": "hour"},
        ])
        return fig

    except Exception as e:
        return _empty_chart(f"Error loading {ticker}: {e}")


def run_dashboard():
    app.run(debug=False, host="0.0.0.0", port=config.DASHBOARD_PORT, use_reloader=False)


if __name__ == "__main__":
    run_dashboard()
