"""Read-only web dashboard for momentum-bot.

A separate, side-effect-free process that shows what the bot is doing:
account equity, open positions, live unrealized P&L, kill-switch status, and
the recent trade ledger. It **never** places, cancels, or modifies orders and
opens the SQLite state file in read-only mode (``mode=ro``), so it cannot
mutate bot state. Safe to expose internally / run alongside the bot.

Run:  python dashboard.py       (serves on DASHBOARD_PORT, default 8080)

Data sources:
  * state.db (read-only): tracked positions, kill switch, day-start equity
  * trades.csv / trades table: realized trade ledger + stats
  * Alpaca (optional, read-only GETs): live equity + unrealized P&L, if keys
    are set and DASHBOARD_LIVE != false. Falls back to local state otherwise.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import config as config_module


# --------------------------------------------------------------------------
# Read-only state access
# --------------------------------------------------------------------------
class StateReader:
    """Opens the bot's SQLite file read-only. Never creates or writes tables."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def available(self) -> bool:
        return os.path.exists(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _meta(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return row["value"] if row else None

    def snapshot(self) -> Dict[str, Any]:
        if not self.available():
            return {"has_state": False}
        try:
            conn = self._conn()
        except sqlite3.Error:
            return {"has_state": False}
        try:
            day_start = self._meta(conn, "day_start_equity")
            positions = [dict(r) for r in
                         conn.execute("SELECT * FROM positions").fetchall()]
            trades = [dict(r) for r in conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT 25").fetchall()]
            stats = self._trade_stats(conn)
            return {
                "has_state": True,
                "day_start_equity": float(day_start) if day_start else None,
                "kill_switch_active": self._meta(conn, "kill_switch_active") == "1",
                "kill_switch_reason": self._meta(conn, "kill_switch_reason") or "",
                "kill_switch_at": self._meta(conn, "kill_switch_at") or "",
                "day_start_date": self._meta(conn, "day_start_date") or "",
                "positions": positions,
                "recent_trades": trades,
                "stats": stats,
                "equity_history": self._equity_history(conn),
            }
        finally:
            conn.close()

    def _equity_history(self, conn: sqlite3.Connection) -> List[list]:
        try:
            rows = conn.execute(
                "SELECT timestamp, equity FROM equity_history ORDER BY id ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [[r["timestamp"], float(r["equity"])] for r in rows]

    def _trade_stats(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        try:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE side='SELL' AND pnl IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            return {"closed": 0, "wins": 0, "win_rate": 0.0, "realized_pnl": 0.0}
        pnls = [float(r["pnl"]) for r in rows]
        wins = [p for p in pnls if p > 0]
        return {
            "closed": len(pnls),
            "wins": len(wins),
            "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
            "realized_pnl": sum(pnls),
        }


# --------------------------------------------------------------------------
# Optional live Alpaca read (read-only GETs only)
# --------------------------------------------------------------------------
def live_account(config) -> Optional[Dict[str, Any]]:
    """Best-effort read-only fetch of live equity + positions. Returns None if
    disabled, keys missing, or Alpaca is unreachable."""
    if os.environ.get("DASHBOARD_LIVE", "true").strip().lower() in {"0", "false", "no"}:
        return None
    if not config.api_key or not config.api_secret:
        return None
    try:
        from broker import Broker
        broker = Broker(config)
        equity = broker.get_equity()
        positions = {sym: {
            "qty": p.qty, "avg_entry_price": p.avg_entry_price,
            "current_price": p.current_price, "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
        } for sym, p in broker.list_positions().items()}
        return {"equity": equity, "positions": positions,
                "exposure": sum(abs(p["market_value"]) for p in positions.values()),
                "unrealized_pl": sum(p["unrealized_pl"] for p in positions.values())}
    except Exception:  # pragma: no cover - network/keys/SDK issues
        return None


# --------------------------------------------------------------------------
# Snapshot assembly (merges state + optional live)
# --------------------------------------------------------------------------
def build_snapshot(config, reader: StateReader) -> Dict[str, Any]:
    snap = reader.snapshot()
    snap["mode"] = config.mode_label
    snap["symbols"] = config.symbols
    snap["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    live = live_account(config)
    snap["live"] = live is not None
    if live:
        snap["equity"] = live["equity"]
        snap["exposure"] = live["exposure"]
        snap["unrealized_pl"] = live["unrealized_pl"]
        snap["live_positions"] = live["positions"]
        ds = snap.get("day_start_equity")
        snap["day_pnl_pct"] = ((live["equity"] - ds) / ds) if ds else None
    return snap


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
def _fmt_money(v: Optional[float]) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:+.2%}" if isinstance(v, (int, float)) else "—"


def _fmt_num(v: Any, digits: int = 6) -> str:
    try:
        return f"{float(v):.{digits}g}"
    except (TypeError, ValueError):
        return "—"


def render_equity_chart(points: List[list], day_start: Optional[float],
                        width: int = 1040, height: int = 150) -> str:
    """Inline SVG equity curve with a dashed day-start baseline.

    Self-contained (no JS/CDN) so it works under a strict CSP. Colours green
    when the latest equity is at/above the day-start baseline, red below.
    """
    if not points or len(points) < 2:
        return ('<div class="muted chart-empty">Equity chart — collecting '
                'data (one sample per cycle)…</div>')

    # Downsample so the polyline stays light regardless of history length.
    max_pts = 240
    if len(points) > max_pts:
        step = len(points) / max_pts
        points = [points[int(i * step)] for i in range(max_pts)]
    vals = [p[1] for p in points]

    lo, hi = min(vals), max(vals)
    if day_start:
        lo, hi = min(lo, day_start), max(hi, day_start)
    if hi == lo:                      # flat line: avoid zero range
        hi = lo + max(1.0, abs(lo) * 0.01)
    pad = (hi - lo) * 0.08
    lo -= pad
    hi += pad

    n = len(vals)
    padL, padR, padT, padB = 8, 8, 10, 12
    plot_w = width - padL - padR
    plot_h = height - padT - padB

    def X(i: int) -> float:
        return padL + plot_w * i / (n - 1)

    def Y(v: float) -> float:
        return padT + plot_h * (1 - (v - lo) / (hi - lo))

    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    base_y = padT + plot_h
    area = f"{X(0):.1f},{base_y:.1f} {line} {X(n - 1):.1f},{base_y:.1f}"

    up = (day_start is None) or (vals[-1] >= day_start)
    stroke = "#3fb950" if up else "#f85149"
    fill = "rgba(63,185,80,0.14)" if up else "rgba(248,81,73,0.14)"

    baseline = ""
    if day_start:
        by = Y(day_start)
        baseline = (f'<line x1="{padL}" y1="{by:.1f}" x2="{padL + plot_w:.1f}" '
                    f'y2="{by:.1f}" stroke="#6e7681" stroke-width="1" '
                    f'stroke-dasharray="4 4"/>')
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img" aria-label="Equity over time">'
        f'<polygon points="{area}" fill="{fill}"/>'
        f'{baseline}'
        f'<polyline points="{line}" fill="none" stroke="{stroke}" '
        f'stroke-width="2" stroke-linejoin="round"/>'
        f'</svg>')


def render_html(snap: Dict[str, Any]) -> str:
    e = html.escape
    mode = snap.get("mode", "PAPER")
    mode_class = "live" if mode == "LIVE" else "paper"
    kill = snap.get("kill_switch_active")

    kill_banner = ""
    if kill:
        kill_banner = (
            f'<div class="banner kill">🚨 KILL SWITCH ACTIVE — trading halted. '
            f'{e(snap.get("kill_switch_reason", ""))}</div>')
    if not snap.get("has_state"):
        kill_banner += ('<div class="banner warn">No state file yet — the bot '
                        'has not run, or STATE_DB_PATH differs.</div>')

    live_note = ("live Alpaca data" if snap.get("live")
                 else "local state only (Alpaca not connected)")

    # Stat cards
    equity = snap.get("equity")
    cards = [
        ("Equity", _fmt_money(equity)),
        ("Day start", _fmt_money(snap.get("day_start_equity"))),
        ("Day P&L", _fmt_pct(snap.get("day_pnl_pct"))),
        ("Exposure", _fmt_money(snap.get("exposure"))),
        ("Unrealized P&L", _fmt_money(snap.get("unrealized_pl"))),
        ("Realized P&L", _fmt_money(snap.get("stats", {}).get("realized_pnl"))),
    ]
    card_html = "".join(
        f'<div class="card"><div class="label">{e(l)}</div>'
        f'<div class="value">{e(v)}</div></div>' for l, v in cards)

    # Positions table (prefer live prices, fall back to tracked stops)
    live_pos = snap.get("live_positions", {})
    rows = []
    for p in snap.get("positions", []):
        sym = p.get("symbol", "")
        lp = live_pos.get(sym, {})
        cur = lp.get("current_price")
        upl = lp.get("unrealized_pl")
        upl_class = "pos" if (isinstance(upl, (int, float)) and upl >= 0) else "neg"
        rows.append(
            f"<tr><td>{e(sym)}</td>"
            f"<td>{_fmt_num(p.get('qty'))}</td>"
            f"<td>{_fmt_num(p.get('entry_price'), 8)}</td>"
            f"<td>{_fmt_num(cur, 8) if cur else '—'}</td>"
            f"<td>{_fmt_num(p.get('stop_price'), 8)}</td>"
            f"<td>{'🔒 trailing' if p.get('trail_active') else 'hard'}</td>"
            f'<td class="{upl_class}">{_fmt_money(upl) if upl is not None else "—"}</td>'
            f"</tr>")
    pos_table = ("".join(rows) if rows else
                 '<tr><td colspan="7" class="muted">No open positions</td></tr>')

    # Recent trades
    trows = []
    for t in snap.get("recent_trades", []):
        pnl = t.get("pnl")
        pnl_class = "" if pnl is None else ("pos" if float(pnl) >= 0 else "neg")
        ts = (t.get("timestamp", "") or "")[:19].replace("T", " ")
        reason = t.get("reason_exit") or t.get("reason_entry") or ""
        trows.append(
            f"<tr><td>{e(ts)}</td><td>{e(t.get('symbol',''))}</td>"
            f"<td>{e(t.get('side',''))}</td><td>{_fmt_num(t.get('qty'))}</td>"
            f"<td>{_fmt_num(t.get('entry'), 8)}</td>"
            f"<td>{_fmt_num(t.get('exit'), 8) if t.get('exit') else '—'}</td>"
            f'<td class="{pnl_class}">{_fmt_money(pnl) if pnl is not None else "—"}</td>'
            f"<td>{e(reason)}</td></tr>")
    trades_table = ("".join(trows) if trows else
                    '<tr><td colspan="8" class="muted">No trades yet</td></tr>')

    stats = snap.get("stats", {})
    win_rate = stats.get("win_rate", 0.0)
    win_line = (f"{win_rate:.0%} win rate ({stats.get('wins',0)}/"
                f"{stats.get('closed',0)} closed)")

    # Equity chart + a small min/current/max caption.
    hist = snap.get("equity_history", [])
    chart_svg = render_equity_chart(hist, snap.get("day_start_equity"))
    chart_caption = ""
    if len(hist) >= 2:
        evals = [h[1] for h in hist]
        chart_caption = (
            f'<span class="muted">low {_fmt_money(min(evals))} · '
            f'now {_fmt_money(evals[-1])} · high {_fmt_money(max(evals))} · '
            f'{len(hist)} samples</span>')

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>momentum-bot · {e(mode)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background:#0d1117; color:#c9d1d9; padding: 20px; }}
  h1 {{ font-size: 18px; margin: 0 0 2px; }}
  .sub {{ color:#8b949e; font-size:12px; margin-bottom:16px; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:10px;
    font-size:12px; font-weight:700; vertical-align:middle; margin-left:8px; }}
  .tag.paper {{ background:#1f6feb33; color:#58a6ff; }}
  .tag.live {{ background:#da363333; color:#f85149; }}
  .banner {{ padding:10px 14px; border-radius:8px; margin-bottom:14px;
    font-weight:600; }}
  .banner.kill {{ background:#da363322; border:1px solid #f85149; color:#f85149; }}
  .banner.warn {{ background:#9e6a0322; border:1px solid #d29922; color:#e3b341; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px; margin-bottom:22px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
    padding:14px; }}
  .card .label {{ color:#8b949e; font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; }}
  .card .value {{ font-size:22px; font-weight:700; margin-top:6px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.5px;
    color:#8b949e; margin:22px 0 8px; }}
  .wrap {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #21262d;
    white-space:nowrap; }}
  th {{ color:#8b949e; font-weight:600; font-size:11px; text-transform:uppercase; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }}
  .chart {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
    padding:8px 6px 2px; margin-bottom:8px; }}
  .chart-empty {{ padding:28px 0; }}
  h2 .muted {{ font-size:11px; text-transform:none; letter-spacing:0;
    font-weight:400; margin-left:6px; }}
  .muted {{ color:#6e7681; text-align:center; font-style:italic; }}
  .foot {{ color:#6e7681; font-size:11px; margin-top:20px; }}
</style></head>
<body>
  <h1>momentum-bot<span class="tag {mode_class}">{e(mode)}</span></h1>
  <div class="sub">Read-only dashboard · {e(live_note)} · {win_line} ·
    updated {e(snap.get("generated_at",""))} (auto-refresh 15s)</div>
  {kill_banner}
  <div class="cards">{card_html}</div>

  <h2>Equity &amp; P&amp;L {chart_caption}</h2>
  <div class="chart">{chart_svg}</div>

  <h2>Open positions</h2>
  <div class="wrap"><table>
    <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Stop</th>
        <th>Stop type</th><th>Unreal. P&L</th></tr>
    {pos_table}
  </table></div>

  <h2>Recent trades</h2>
  <div class="wrap"><table>
    <tr><th>Time (UTC)</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th>
        <th>Exit</th><th>P&L</th><th>Reason</th></tr>
    {trades_table}
  </table></div>

  <div class="foot">Read-only view. This page never places or modifies orders.
    Symbols watched: {e(", ".join(snap.get("symbols", [])))}</div>
</body></html>"""


# --------------------------------------------------------------------------
# HTTP server (GET-only)
# --------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    config = None
    reader = None

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            snap = build_snapshot(self.config, self.reader)
            self._send(200, render_html(snap).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            snap = build_snapshot(self.config, self.reader)
            self._send(200, json.dumps(snap, default=str).encode("utf-8"),
                       "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    # Read-only: explicitly reject any mutating method.
    def do_POST(self):   # noqa: N802
        self._send(405, b"read-only dashboard", "text/plain")
    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, *args) -> None:  # quiet access log
        return


def main() -> None:
    config = config_module.load_config()
    reader = StateReader(config.state_db_path)
    DashboardHandler.config = config
    DashboardHandler.reader = reader
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"momentum-bot dashboard (read-only) on http://{host}:{port}  "
          f"[mode={config.mode_label}, state={config.state_db_path}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
