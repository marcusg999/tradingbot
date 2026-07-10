import os

import pytest

import config as config_module
import dashboard
from state import PositionRecord, State


@pytest.fixture
def populated(tmp_path, monkeypatch):
    # Disable any live Alpaca fetch so the dashboard reads local state only.
    monkeypatch.setenv("DASHBOARD_LIVE", "false")
    db = str(tmp_path / "state.db")
    csv = str(tmp_path / "trades.csv")
    st = State(db, csv)
    st.roll_day_if_needed(10_000.0)
    st.upsert_position(PositionRecord(
        symbol="BTC/USD", qty=0.02, entry_price=60000, stop_price=58200,
        stop_order_id="o1", high_water_mark=61000, trail_active=True,
        opened_at="2026-07-10T12:00:00+00:00"))
    st.record_trade(symbol="ETH/USD", side="BUY", qty=0.5, entry=3400,
                    exit=None, stop=3298, pnl=None,
                    reason_entry="ema_cross_up_rsi_ok", reason_exit="")
    st.record_trade(symbol="ETH/USD", side="SELL", qty=0.5, entry=3400,
                    exit=3550, stop=3298, pnl=75.0, reason_entry="",
                    reason_exit="ema_cross_down")
    st.record_trade(symbol="SOL/USD", side="SELL", qty=2.0, entry=150,
                    exit=145, stop=145.5, pnl=-10.0, reason_entry="",
                    reason_exit="stop_filled")
    st.close()
    monkeypatch.setenv("STATE_DB_PATH", db)
    monkeypatch.setenv("TRADES_CSV_PATH", csv)
    return config_module.load_config(), dashboard.StateReader(db)


def test_reader_missing_file_is_graceful(tmp_path):
    reader = dashboard.StateReader(str(tmp_path / "nope.db"))
    assert reader.available() is False
    assert reader.snapshot() == {"has_state": False}


def test_snapshot_reads_state(populated):
    cfg, reader = populated
    snap = dashboard.build_snapshot(cfg, reader)
    assert snap["has_state"] is True
    assert snap["day_start_equity"] == 10_000.0
    assert snap["kill_switch_active"] is False
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["symbol"] == "BTC/USD"
    assert snap["live"] is False  # DASHBOARD_LIVE=false


def test_snapshot_trade_stats(populated):
    cfg, reader = populated
    snap = dashboard.build_snapshot(cfg, reader)
    stats = snap["stats"]
    # Two closed SELLs with pnl: +75 and -10 -> 1 win of 2, +65 realized.
    assert stats["closed"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate"] == pytest.approx(0.5)
    assert stats["realized_pnl"] == pytest.approx(65.0)


def test_render_html_is_valid_and_readonly(populated):
    cfg, reader = populated
    snap = dashboard.build_snapshot(cfg, reader)
    page = dashboard.render_html(snap)
    assert page.startswith("<!doctype html>")
    assert "momentum-bot" in page
    assert "BTC/USD" in page and "ema_cross_down" in page
    assert "Read-only view" in page
    # Auto-refresh present; no form/POST controls in a read-only page.
    assert 'http-equiv="refresh"' in page
    assert "<form" not in page.lower()


def test_kill_switch_banner_renders(populated, tmp_path):
    cfg, reader = populated
    # Flip the kill switch in a fresh writable connection, then re-read.
    st = State(reader.db_path, str(tmp_path / "t2.csv"))
    st.set_kill_switch(True, "equity -5.2% test halt")
    st.close()
    snap = dashboard.build_snapshot(cfg, reader)
    assert snap["kill_switch_active"] is True
    page = dashboard.render_html(snap)
    assert "KILL SWITCH ACTIVE" in page
    assert "equity -5.2% test halt" in page


def test_html_escapes_reason_text(populated, tmp_path):
    cfg, reader = populated
    st = State(reader.db_path, str(tmp_path / "t3.csv"))
    st.set_kill_switch(True, "<script>alert(1)</script>")
    st.close()
    snap = dashboard.build_snapshot(cfg, reader)
    page = dashboard.render_html(snap)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
