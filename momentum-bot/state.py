"""Persistent state via SQLite + append-only trades CSV.

Alpaca is always the source of truth for what positions/orders exist. This
store holds the *bot's* private bookkeeping that Alpaca cannot tell us:
  * the day's starting equity (for the kill switch)
  * kill-switch status
  * per-position stop metadata (stop order id, high-water mark, trail state)
  * a durable trade ledger

All timestamps are stored as ISO-8601 UTC strings.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Dict, List, Optional


# Retention cap for the equity-history table (~2 days at one sample/minute).
MAX_EQUITY_POINTS = 3000

_CENT = Decimal("0.01")


def quantize_money(value: Optional[float]) -> Optional[float]:
    """Round a USD amount to whole cents via Decimal so the ledger foots
    exactly instead of accumulating binary-float error across trades."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_EVEN))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PositionRecord:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    stop_order_id: Optional[str]
    high_water_mark: float
    trail_active: bool
    opened_at: str


TRADE_COLUMNS = [
    "timestamp", "symbol", "side", "qty", "entry", "exit", "stop",
    "pnl", "reason_entry", "reason_exit",
]


class State:
    def __init__(self, db_path: str, trades_csv_path: str) -> None:
        self.db_path = db_path
        self.trades_csv_path = trades_csv_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._ensure_csv_header()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                stop_order_id TEXT,
                high_water_mark REAL NOT NULL,
                trail_active INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry REAL,
                exit REAL,
                stop REAL,
                pnl REAL,
                reason_entry TEXT,
                reason_exit TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                equity REAL NOT NULL
            )""")
        self.conn.commit()

    def _ensure_csv_header(self) -> None:
        if not os.path.exists(self.trades_csv_path) or \
                os.path.getsize(self.trades_csv_path) == 0:
            with open(self.trades_csv_path, "w", newline="") as fh:
                csv.writer(fh).writerow(TRADE_COLUMNS)

    # --- meta key/value --------------------------------------------------
    def _get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self.conn.commit()

    # --- daily equity / kill switch -------------------------------------
    def get_day_start(self) -> Optional[tuple]:
        """Return (date_str, equity) for the recorded trading day, or None."""
        date = self._get_meta("day_start_date")
        equity = self._get_meta("day_start_equity")
        if date is None or equity is None:
            return None
        return date, float(equity)

    def roll_day_if_needed(self, current_equity: float) -> float:
        """Ensure day-start equity is set for today (UTC). Returns it.

        Deliberately does NOT touch the kill switch: a triggered halt persists
        across day rolls and process restarts until an operator clears it
        (main.py --reset-kill-switch or RESET_KILL_SWITCH=yes).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = self.get_day_start()
        if existing is None or existing[0] != today:
            self._set_meta("day_start_date", today)
            self._set_meta("day_start_equity", str(current_equity))
            return current_equity
        return existing[1]

    def is_kill_switch_active(self) -> bool:
        return self._get_meta("kill_switch_active") == "1"

    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        self._set_meta("kill_switch_active", "1" if active else "0")
        self._set_meta("kill_switch_reason", reason)
        if active:
            self._set_meta("kill_switch_at", _utcnow_iso())

    def kill_switch_reason(self) -> str:
        return self._get_meta("kill_switch_reason") or ""

    # --- positions -------------------------------------------------------
    def upsert_position(self, rec: PositionRecord) -> None:
        self.conn.execute("""
            INSERT INTO positions(symbol, qty, entry_price, stop_price,
                stop_order_id, high_water_mark, trail_active, opened_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                qty=excluded.qty,
                entry_price=excluded.entry_price,
                stop_price=excluded.stop_price,
                stop_order_id=excluded.stop_order_id,
                high_water_mark=excluded.high_water_mark,
                trail_active=excluded.trail_active,
                opened_at=excluded.opened_at
        """, (rec.symbol, rec.qty, rec.entry_price, rec.stop_price,
              rec.stop_order_id, rec.high_water_mark,
              1 if rec.trail_active else 0, rec.opened_at))
        self.conn.commit()

    def get_position(self, symbol: str) -> Optional[PositionRecord]:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_position(row) if row else None

    def all_positions(self) -> Dict[str, PositionRecord]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return {r["symbol"]: self._row_to_position(r) for r in rows}

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PositionRecord:
        return PositionRecord(
            symbol=row["symbol"],
            qty=row["qty"],
            entry_price=row["entry_price"],
            stop_price=row["stop_price"],
            stop_order_id=row["stop_order_id"],
            high_water_mark=row["high_water_mark"],
            trail_active=bool(row["trail_active"]),
            opened_at=row["opened_at"],
        )

    # --- trade ledger ----------------------------------------------------
    def record_trade(self, *, symbol: str, side: str, qty: float,
                     entry: Optional[float], exit: Optional[float],
                     stop: Optional[float], pnl: Optional[float],
                     reason_entry: str = "", reason_exit: str = "") -> None:
        ts = _utcnow_iso()
        pnl = quantize_money(pnl)  # foot the ledger to exact cents
        self.conn.execute("""
            INSERT INTO trades(timestamp, symbol, side, qty, entry, exit,
                stop, pnl, reason_entry, reason_exit)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ts, symbol, side, qty, entry, exit, stop, pnl,
              reason_entry, reason_exit))
        self.conn.commit()
        with open(self.trades_csv_path, "a", newline="") as fh:
            csv.writer(fh).writerow([ts, symbol, side, qty, entry, exit,
                                     stop, pnl, reason_entry, reason_exit])

    # --- equity history (for the dashboard chart) -----------------------
    def record_equity(self, equity: float, ts: Optional[str] = None) -> None:
        """Append an equity sample and prune to the retention cap."""
        ts = ts or _utcnow_iso()
        self.conn.execute(
            "INSERT INTO equity_history(timestamp, equity) VALUES(?, ?)",
            (ts, equity))
        # Keep the table bounded (roughly MAX_EQUITY_POINTS most-recent rows).
        self.conn.execute(
            "DELETE FROM equity_history WHERE id <= "
            "(SELECT MAX(id) FROM equity_history) - ?", (MAX_EQUITY_POINTS,))
        self.conn.commit()

    def equity_history(self, limit: Optional[int] = None):
        """Return [(timestamp, equity), ...] oldest-first."""
        q = "SELECT timestamp, equity FROM equity_history ORDER BY id ASC"
        rows = self.conn.execute(q).fetchall()
        out = [(r["timestamp"], float(r["equity"])) for r in rows]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def close(self) -> None:
        self.conn.close()
