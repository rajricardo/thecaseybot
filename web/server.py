"""
Casey Bridge: the control UI's Flask backend. Runs in its own daemon
thread (started from bot.py, mirroring trade_executor.run_worker's
thread-per-concern shape), bound to 127.0.0.1 only — this can trigger real
IBKR orders, so it must never be reachable off this machine.

Hard rule this module exists to respect: only trade_executor.run_worker's
thread ever calls into ib_async. This module never imports ibkr_client and
never touches the `ib` connection object — driving ib_async from a second
OS thread is unsupported and unsafe. Instead this reads/writes:
  - db.py's SQLite ledger (positions, signal feed, order history, round
    trips) — the worker thread publishes into it, this only ever reads it,
    except for the small bot_state keys (paused) this module owns outright.
  - config.yaml, via ruamel.yaml (round-trip: preserves the file's existing
    comments and formatting on write, unlike plain PyYAML's dump). risk
    edits are picked up by the next signal (load_risk_config re-reads
    fresh); discord/ibkr/llm edits need a bot restart, same as today.
  - signal_queue (thread-safe queue.Queue), for manual ADD/TRIM/CLOSE —
    pushed through as a synthetic Signal so handle_trim/handle_add/
    handle_exit run completely unchanged. No order logic lives here.
  - validation_queue, for the "+ add ticker" flow on the Settings screen —
    a request/response handoff (TickerValidationRequest below) to the
    worker thread for a real IBKR check (does the symbol exist, does it
    have listed options) before a typed ticker is added to
    risk.allowed_tickers. This Flask thread blocks the HTTP request on
    req.event.wait(), not the worker thread's loop.
"""

import itertools
import logging
import random
import threading
import time
from datetime import date, datetime

from flask import Flask, jsonify, request
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

import db
from alerting import log_signal
from signal_classifier import Signal, SignalType

_yaml = YAML()
_yaml.preserve_quotes = True
_config_lock = threading.Lock()

HOST = "127.0.0.1"
PORT = 8787
TICKER_VALIDATION_TIMEOUT = 10  # seconds to wait for the worker thread's IBKR check

# app.js polls /api/state every 2.5s (POLL_MS) — left at werkzeug's default
# access-log verbosity that's a "127.0.0.1 - - [...] GET /api/state 200 -"
# line every single poll. _QuietStatePolls below silences just that one
# noisy endpoint from werkzeug's own logger; the heartbeat print in
# create_app's after_request hook replaces it with something readable,
# throttled to roughly every HEARTBEAT_EVERY_N_POLLS polls (~10s at the
# default cadence) so the console stays calm rather than swapping one kind
# of spam for another. Printed straight to stdout, not routed through the
# casey_bot logger/casey_bot.log — that file is meant to stay a clean
# signal/order audit trail (see alerting.py), not a heartbeat feed.
HEARTBEAT_EVERY_N_POLLS = 4

_HEARTBEAT_PHRASES = [
    "Casey Bridge is watching the tape... 👀",
    "Still here, still watching for Casey's next move.",
    "Scanning the channel — all quiet for now.",
    "Dashboard pinged. Bot's awake and caffeinated.",
    "Keeping an eye on things while you do literally anything else.",
    "All systems nominal. No trades, no drama.",
    "Watching, waiting, not trading (yet).",
    "Heartbeat OK — Casey hasn't said anything actionable.",
    "Casey Bridge: online and mildly bored.",
]


class _QuietStatePolls(logging.Filter):
    """Drops werkzeug's default access-log line for GET /api/state (the
    dashboard's poll endpoint) so it doesn't fight with the heartbeat print
    below for the same line of console real estate. Every other
    request/response — including errors — still logs normally."""

    def filter(self, record):
        return "/api/state" not in record.getMessage()


class TickerValidationRequest:
    """A "+ add ticker" request handed to the worker thread via
    validation_queue (see trade_executor.run_worker's docstring) — this
    thread fills in .ticker and waits on .event; the worker thread fills in
    .valid/.reason and sets .event. Duck-typed on the trade_executor side
    (no import of this class there) to keep the two modules loosely coupled."""

    def __init__(self, ticker):
        self.ticker = ticker
        self.event = threading.Event()
        self.valid = False
        self.reason = ""


def _snowflake_str(value):
    """str() a Discord snowflake ID for JSON transport: these are 18-19
    digits, past Number.MAX_SAFE_INTEGER, so a bare JSON number gets
    silently rounded by JS's float64 on the way into the browser. Python
    itself never has this problem (arbitrary-precision ints) — it's purely
    a JSON-transport concern, hence converting right at the JSON boundary."""
    return str(value) if value is not None else None


def _read_config(config_path):
    with open(config_path) as f:
        return _yaml.load(f)


def _write_config(config_path, data):
    with open(config_path, "w") as f:
        _yaml.dump(data, f)


def _patch_config(config_path, patch):
    """Shallow-merges patch's per-section keys into config.yaml in place.
    Unknown sections, non-dict section values, and keys that don't already
    exist in that section are all ignored rather than creating new ones —
    this only ever edits values the Settings screen already knows about.

    List-valued keys (allowed_tickers) are updated in place — clear() then
    extend() — rather than reassigned. ruamel.yaml remembers that
    allowed_tickers was written as a flow-style `[...]` list on the
    CommentedSeq object itself; replacing it with a plain Python list would
    lose that and silently reformat it to one-item-per-line block style on
    the next save."""
    with _config_lock:
        data = _read_config(config_path)
        for section, values in patch.items():
            if section not in data or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key not in data[section]:
                    continue
                existing = data[section][key]
                if isinstance(existing, list) and isinstance(value, list):
                    existing.clear()
                    existing.extend(value)
                else:
                    data[section][key] = value
        _write_config(config_path, data)


def _kpis(trips):
    pnls = [t["realized_pnl"] for t in trips if t["realized_pnl"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)
    today = date.today()
    realized_today = sum(
        t["realized_pnl"] for t in trips
        if t["realized_pnl"] is not None
        and datetime.fromtimestamp(t["closed_ts"]).date() == today
    )
    return {
        "net_pnl": round(net, 2),
        "trade_count": len(pnls),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "win_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "n_wins": len(wins), "n_losses": len(losses), "n_flat": len(flat),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "realized_today": round(realized_today, 2),
    }


def _ibkr_connected():
    """Heuristic, not a live check: True if the worker thread's position
    snapshot (trade_executor._snapshot_state) has run in the last 10s. That
    loop only succeeds while actually connected to TWS/Gateway, so a stale
    heartbeat means the connection is down (or reconnecting — see
    ibkr_client.ensure_connected) without this (Flask) thread ever touching
    ib_async itself to find out."""
    ibkr_last = db.get_bot_state("ibkr_last_snapshot_ts")
    return ibkr_last is not None and (time.time() - float(ibkr_last)) < 10


def _health(discord_cfg):
    last_msg = db.get_bot_state("discord_last_message_ts")
    ibkr_last = db.get_bot_state("ibkr_last_snapshot_ts")
    return {
        "discord": {
            "connected": db.get_bot_state("discord_connected") == "1",
            "channel": _snowflake_str(discord_cfg.get("channel_id")),
            "last_message_ts": float(last_msg) if last_msg else None,
        },
        "claude": {"call_count": int(db.get_bot_state("claude_call_count", "0") or 0)},
        "ibkr": {
            "connected": _ibkr_connected(),
            "last_snapshot_ts": float(ibkr_last) if ibkr_last else None,
        },
    }


def _week_pnl(trips):
    """Realized P&L per day for the last 5 calendar days (mirrors the
    design's week strip; simplified to always show the trailing 5 days
    rather than the full weekday grid — that distinction isn't tracked
    anywhere and isn't worth a new table)."""
    by_day = {}
    for t in trips:
        if t["realized_pnl"] is None:
            continue
        d = datetime.fromtimestamp(t["closed_ts"]).date()
        by_day[d] = by_day.get(d, 0.0) + t["realized_pnl"]
    today = date.today()
    days = [today.fromordinal(today.toordinal() - i) for i in range(4, -1, -1)]
    return [
        {"date": d.isoformat(), "label": d.strftime("%a %d"), "is_today": d == today,
         "pnl": round(by_day.get(d, 0.0), 2) if d in by_day else None}
        for d in days
    ]


def _add_allowed_ticker(config_path, ticker):
    """Atomic add: a single lock/read/write, not a read-then-later-patch —
    the IBKR validation round trip this is called after can take up to
    TICKER_VALIDATION_TIMEOUT seconds, during which another request could
    have changed the list, so the presence check has to happen fresh, right
    here under the lock, not against a snapshot taken before the wait.
    Mutates the CommentedSeq in place (append), same reasoning as
    _patch_config's clear()+extend() — never reassign the list wholesale,
    or ruamel loses the original flow-style `[...]` formatting. Returns
    False if the ticker was already present (a no-op, not an error).

    Appended as a DoubleQuotedScalarString, not a plain str: ruamel has no
    quote-style memory for a value that was never part of the parsed
    document, so a plain str appended here would come out unquoted
    (`[..., META]` next to the existing `"SPY"`, `"QQQ"`) — inconsistent
    with the rest of the list, and not just cosmetically: YAML's default
    resolver reads unquoted `on`/`off`/`yes`/`no`/`true`/`false` as
    booleans, not strings, so an unquoted ticker that happened to collide
    with one of those words would silently stop being a string on the next
    load."""
    with _config_lock:
        data = _read_config(config_path)
        tickers = data.get("risk", {}).get("allowed_tickers")
        if tickers is None or ticker in tickers:
            return False
        tickers.append(DoubleQuotedScalarString(ticker))
        _write_config(config_path, data)
        return True


def _remove_allowed_ticker(config_path, ticker):
    with _config_lock:
        data = _read_config(config_path)
        tickers = data.get("risk", {}).get("allowed_tickers")
        if not tickers or ticker not in tickers:
            return False
        tickers.remove(ticker)
        _write_config(config_path, data)
        return True


def _settings_payload(config):
    risk = dict(config.get("risk", {}))
    discord_cfg = config.get("discord", {})
    ibkr_cfg = config.get("ibkr", {})
    llm_cfg = config.get("llm", {})
    reconnect_cfg = config.get("reconnect", {})
    return {
        "risk": {**risk, "allowed_tickers": list(risk.get("allowed_tickers") or [])},
        "discord": {
            "channel_id": _snowflake_str(discord_cfg.get("channel_id")),
            "casey_user_id": _snowflake_str(discord_cfg.get("casey_user_id")),
            "user_token_set": bool(discord_cfg.get("user_token")),
        },
        "ibkr": {
            "host": ibkr_cfg.get("host"), "port": ibkr_cfg.get("port"),
            "client_id": ibkr_cfg.get("client_id"),
        },
        "llm": {"model": llm_cfg.get("model"), "api_key_set": bool(llm_cfg.get("api_key"))},
        # same defaults as trade_executor.load_reconnect_config, so the UI
        # shows the behavior a missing/partial reconnect: section actually
        # falls back to rather than a misleading blank/off state. The
        # Settings screen no longer exposes an "unbounded timeout" state
        # (retry_timeout_mins: null in config.yaml is still honored by
        # trade_executor if hand-edited, but Save always writes a concrete
        # number) — this or-5 keeps a null/missing value from rendering as
        # a blank input for a leftover pre-this-change config.yaml.
        "reconnect": {
            "retry_on_reconnect": reconnect_cfg.get("retry_on_reconnect", True),
            "retry_timeout_mins": reconnect_cfg.get("retry_timeout_mins") or 5,
        },
    }


def create_app(config_path, signal_queue, validation_queue, logger):
    app = Flask(__name__, static_folder="static", static_url_path="")

    _poll_count = itertools.count()

    @app.after_request
    def _heartbeat(response):
        """Replaces the raw werkzeug access-log line for /api/state polls
        (silenced by _QuietStatePolls) with an occasional fun one-liner —
        see HEARTBEAT_EVERY_N_POLLS/_HEARTBEAT_PHRASES above for why this
        prints straight to stdout rather than going through casey_bot.log."""
        if request.path == "/api/state" and request.method == "GET":
            n = next(_poll_count)
            if n % HEARTBEAT_EVERY_N_POLLS == 0:
                print(f"[Casey Bridge] {random.choice(_HEARTBEAT_PHRASES)}", flush=True)
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def api_state():
        config = _read_config(config_path)
        risk = dict(config.get("risk", {}))
        discord_cfg = config.get("discord", {})

        paused = db.get_paused()
        live = not risk.get("require_confirmation", True)
        status_text = (
            ("Bot running" if not paused else "Bot stopped")
            + " · " + ("LIVE — orders reach IBKR" if live else "Dry run — orders logged only")
            + f" · watching channel {discord_cfg.get('channel_id')}, "
            + f"author {discord_cfg.get('casey_user_id')}"
        )

        # fetched once and reused below — _kpis/_week_pnl/history all derive
        # from the same round trips, so this is the one query per request
        # that needs the largest window (5000) rather than three separate
        # ones each opening their own connection.
        trips = db.get_round_trips(limit=5000)

        return jsonify({
            "bar": {"paused": paused, "mode": "live" if live else "dry", "status_text": status_text},
            "health": _health(discord_cfg),
            "kpis": _kpis(trips),
            "positions": db.get_positions(),
            "feed": db.get_recent_signals(limit=300),
            "history": trips[:100],
            "week": _week_pnl(trips),
            "settings": _settings_payload(config),
        })

    @app.get("/api/settings")
    def get_settings():
        return jsonify(_settings_payload(_read_config(config_path)))

    @app.post("/api/settings")
    def post_settings():
        body = request.get_json(force=True, silent=True) or {}
        patch = {}
        if "risk" in body:
            patch["risk"] = body["risk"]
        if "ibkr" in body:
            patch["ibkr"] = body["ibkr"]
        if "reconnect" in body:
            patch["reconnect"] = body["reconnect"]
        if "discord" in body:
            d = dict(body["discord"])
            token = d.pop("user_token", None)
            if token:
                d["user_token"] = token
            # channel_id/casey_user_id arrive as strings (see
            # _settings_payload's comment) — int() here is exact regardless
            # of digit count, unlike a JS-side numeric coercion would be.
            for key in ("channel_id", "casey_user_id"):
                if key in d and d[key] not in (None, ""):
                    d[key] = int(d[key])
                elif key in d:
                    del d[key]
            patch["discord"] = d
        if "llm" in body:
            m = dict(body["llm"])
            key = m.pop("api_key", None)
            if key:
                m["api_key"] = key
            patch["llm"] = m
        _patch_config(config_path, patch)
        return jsonify(ok=True)

    @app.post("/api/mode")
    def post_mode():
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode")
        if mode not in ("dry", "live"):
            return jsonify(ok=False, error="mode must be 'dry' or 'live'"), 400
        _patch_config(config_path, {"risk": {"require_confirmation": mode == "dry"}})
        return jsonify(ok=True)

    @app.post("/api/bot/pause")
    def post_pause():
        db.set_paused(True)
        return jsonify(ok=True)

    @app.post("/api/bot/resume")
    def post_resume():
        db.set_paused(False)
        return jsonify(ok=True)

    @app.post("/api/positions/<ticker>/<action>")
    def post_position_action(ticker, action):
        sig_type = {"trim": SignalType.TRIM, "add": SignalType.ADD, "close": SignalType.EXIT}.get(action)
        if sig_type is None:
            return jsonify(ok=False, error="unknown action"), 404

        signal = Signal(type=sig_type, ticker=ticker, reason="Manual UI action",
                         raw_text="[Manual UI action]")
        blocked_reason = "bot paused" if db.get_paused() else None
        signal.db_id = log_signal(logger, signal, "manual", blocked_reason)

        if blocked_reason:
            return jsonify(ok=False, error=blocked_reason), 409

        signal_queue.put(signal)
        return jsonify(ok=True)

    @app.post("/api/tickers")
    def post_add_ticker():
        body = request.get_json(force=True, silent=True) or {}
        ticker = (body.get("ticker") or "").strip().upper()
        if not ticker:
            return jsonify(ok=False, reason="Enter a ticker symbol"), 400

        current = list(_read_config(config_path).get("risk", {}).get("allowed_tickers") or [])
        if ticker in current:
            return jsonify(ok=False, reason=f"{ticker} is already in the allowed list"), 400

        if not _ibkr_connected():
            return jsonify(ok=False, reason="IBKR isn't connected right now — start the bot "
                            "and make sure Gateway/TWS is running, then try again."), 409

        req = TickerValidationRequest(ticker)
        validation_queue.put(req)
        if not req.event.wait(timeout=TICKER_VALIDATION_TIMEOUT):
            return jsonify(ok=False, reason="Validation timed out — try again."), 504

        if not req.valid:
            return jsonify(ok=False, reason=req.reason), 400

        _add_allowed_ticker(config_path, ticker)
        return jsonify(ok=True, ticker=ticker)

    @app.post("/api/tickers/<ticker>/remove")
    def post_remove_ticker(ticker):
        ticker = ticker.strip().upper()
        current = list(_read_config(config_path).get("risk", {}).get("allowed_tickers") or [])
        if ticker in current and len(current) <= 1:
            return jsonify(ok=False, reason="Can't remove the last allowed ticker — an empty "
                            "list disables the ticker restriction entirely, letting ENTRY "
                            "signals through for any ticker (see handle_entry)."), 400
        removed = _remove_allowed_ticker(config_path, ticker)
        return jsonify(ok=removed)

    return app


def run_web(config_path, config, signal_queue, validation_queue, logger):
    logging.getLogger("werkzeug").addFilter(_QuietStatePolls())
    app = create_app(config_path, signal_queue, validation_queue, logger)
    # use_reloader=False is required: Flask's reloader forks a subprocess,
    # which would break this thread-embedded-in-bot.py model entirely.
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
