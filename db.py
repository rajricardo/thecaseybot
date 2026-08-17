"""
SQLite ledger backing the Casey Bridge web UI (web/server.py). This is the
project's first persistent store — everything else (daily_risk_stats() in
ibkr_client.py, the signal_queue) is deliberately in-memory, reset on
restart, per how this bot was scoped pre-live-trading. The UI needs to
survive restarts and be readable from a thread that must never touch
ib_async directly (see web/server.py's docstring), so it gets its own store.

Every function opens and closes a short-lived connection rather than
sharing one across threads: the Discord asyncio thread, the IBKR worker
thread, and every Flask request thread all call into this module
concurrently. WAL journal mode + a busy_timeout let concurrent readers and
the occasional overlapping writer coexist without a shared connection
object or an application-level lock.
"""

import contextlib
import json
import sqlite3
import time

DB_PATH = "casey_bridge.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    ticker TEXT,
    direction TEXT,
    reason TEXT,
    raw_text TEXT,
    stage TEXT,
    blocked_reason TEXT,
    outcome_text TEXT,
    outcome_instrument TEXT,
    outcome_failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    signal_id INTEGER,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    ticker TEXT,
    contract_label TEXT,
    qty INTEGER,
    price_type TEXT,
    ibkr_order_id INTEGER,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS round_trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    closed_ts REAL NOT NULL,
    ticker TEXT,
    contract_label TEXT,
    realized_pnl REAL,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_round_trips_ts ON round_trips(closed_ts);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    ticker TEXT PRIMARY KEY,
    contract_label TEXT,
    qty INTEGER,
    avg REAL,
    last REAL,
    unrealized_pnl REAL,
    stop_desc TEXT,
    updated_ts REAL
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


_initialized = False


@contextlib.contextmanager
def _conn():
    global _initialized
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if not _initialized:
        # Lazy, idempotent schema creation — every function in this module
        # goes through here, so ibkr_client/trade_executor/tests that touch
        # db.py without bot.py's explicit init_db() (e.g. test_ibkr_client.py,
        # which never runs bot.py's startup) still get working tables
        # instead of "no such table" on first write.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES ('paused', '0')")
        conn.commit()
        _initialized = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Explicit entrypoint bot.py calls at startup — same lazy/idempotent
    schema creation _conn() does on first use either way, called out here
    so bot.py's startup sequence stays legible."""
    with _conn():
        pass


# ── signals ──────────────────────────────────────────────────────────────

def insert_signal(sig_type, ticker, direction, reason, raw_text, stage, blocked_reason=None):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO signals (ts, type, ticker, direction, reason, raw_text, stage, "
            "blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), sig_type, ticker, direction, reason, raw_text, stage, blocked_reason),
        )
        return cur.lastrowid


def update_signal_outcome(signal_id, outcome_text, outcome_instrument=None, failed=False):
    if signal_id is None:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE signals SET outcome_text = ?, outcome_instrument = ?, outcome_failed = ? "
            "WHERE id = ?",
            (outcome_text, outcome_instrument, int(failed), signal_id),
        )


def get_recent_signals(limit=200):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_raw_texts(limit=5):
    """The last `limit` messages' raw_text, oldest first — threaded into
    llm_classifier.classify() as ticker-disambiguation context (e.g. "adding
    back my 774p here" right after a message naming SPY). Called before the
    current message is logged (see bot.py's on_message_text), so this never
    includes the message currently being classified. Every classified
    message ends up in this table regardless of type (NOISE included), so
    this naturally captures plain commentary that named a ticker too."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT raw_text FROM signals WHERE raw_text IS NOT NULL AND raw_text != '' "
            "ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["raw_text"] for r in reversed(rows)]


# ── orders ───────────────────────────────────────────────────────────────

def insert_order(signal_id, source, action, ticker, contract_label, qty, price_type,
                  ibkr_order_id=None, status="submitted", detail=""):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO orders (ts, signal_id, source, action, ticker, contract_label, qty, "
            "price_type, ibkr_order_id, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), signal_id, source, action, ticker, contract_label, qty, price_type,
             ibkr_order_id, status, detail),
        )
        return cur.lastrowid


def get_recent_orders(limit=200):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── round trips (closed positions) ──────────────────────────────────────

def insert_round_trip(ticker, contract_label, realized_pnl, mode="LIVE"):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO round_trips (closed_ts, ticker, contract_label, realized_pnl, mode) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), ticker, contract_label, realized_pnl, mode),
        )


def get_round_trips(limit=100):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM round_trips ORDER BY closed_ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── live position snapshot (written by the IBKR worker thread only,
#    read by the Flask thread — this is how the UI sees position state
#    without ever calling ib_async off the worker thread) ────────────────

def snapshot_positions(rows):
    """rows: list of dicts with ticker, contract_label, qty, avg, last,
    unrealized_pnl, stop_desc. Replaces the table wholesale — the current
    open-position set at IBKR, not an append-only log."""
    with _conn() as conn:
        conn.execute("DELETE FROM positions_snapshot")
        conn.executemany(
            "INSERT INTO positions_snapshot (ticker, contract_label, qty, avg, last, "
            "unrealized_pnl, stop_desc, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r["ticker"], r["contract_label"], r["qty"], r["avg"], r["last"],
                 r["unrealized_pnl"], r.get("stop_desc"), time.time())
                for r in rows
            ],
        )


def get_positions():
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM positions_snapshot ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]


# ── misc bot state (pause flag, health heartbeats) ──────────────────────

def get_bot_state(key, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_bot_state(key, value):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_paused():
    return get_bot_state("paused", "0") == "1"


def set_paused(paused):
    set_bot_state("paused", "1" if paused else "0")


def increment_counter(key):
    """A single UPSERT rather than read-then-write: the latter would race
    if this were ever called from more than one thread (today it's only
    called from bot.py's single Discord asyncio thread, so it's currently
    safe either way, but this way it's safe by construction, not by
    coincidence)."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (key,),
        )


def get_bot_state_json(key, default=None):
    raw = get_bot_state(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def set_bot_state_json(key, value):
    set_bot_state(key, json.dumps(value))
