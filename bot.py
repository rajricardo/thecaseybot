"""
Entrypoint: Discord -> classify -> log, and ENTRY/EXIT/TRIM/ADD signals ->
IBKR order via trade_executor (risk-gated, gated by
risk.require_confirmation). The IBKR side runs on its own thread with its
own event loop — see trade_executor.run_worker for why it's not shared with
the Discord listener. A third thread (web.server.run_web) serves the Casey
Bridge control UI on 127.0.0.1 — it never touches ib_async directly (only
the worker thread does), it only reads/writes db.py's SQLite ledger,
config.yaml, and this module's signal_queue/validation_queue (thread-safe
queue.Queue). validation_queue carries the web UI's "+ add ticker"
requests through to the worker thread for a real IBKR check before a typed
symbol is added to risk.allowed_tickers — same request-routing reason as
signal_queue, just request/response instead of fire-and-forget.

Classification is two-stage: signal_classifier.classify() (regex, ENTRY/EXIT
only) first; anything it can't confidently place (None) falls through to
llm_classifier.classify() (Claude, decides EXIT/TRIM/ADD/NOISE) — see both
modules' docstrings for why the split. discord_listener awaits
on_message_text so the LLM call (a real network round trip) never blocks
the same event loop carrying the Discord connection's heartbeat.

Every classified message is logged to db.signals via alerting.log_signal
regardless of the UI's pause state, so the Signal feed stays a complete
record of what Casey said — pausing only stops NOISE-filtered signals from
reaching signal_queue (see db.get_paused()/the UI's Stop control), it never
stops classification/logging.

If risk.require_confirmation is true (the default), entries/trims/exits are
derived and logged but never actually submitted to IBKR — see
trade_executor's handle_entry/handle_trim/handle_exit.
"""

import queue
import sys
import threading
import time

import anthropic
import yaml

import db
import discord_listener
import llm_classifier
import trade_executor
import web.server
from alerting import build_logger, log_signal
from signal_classifier import SignalType, classify


CONFIG_PATH = "config.yaml"


def main():
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    discord_cfg = config["discord"]
    bot_cfg = config["bot"]
    llm_cfg = config["llm"]

    db.init_db()
    logger = build_logger(bot_cfg["log_file"])
    llm_client = anthropic.AsyncAnthropic(api_key=llm_cfg["api_key"])
    llm_model = llm_cfg["model"]

    signal_queue = queue.Queue()
    validation_queue = queue.Queue()  # web UI's "+ add ticker" -> worker thread's IBKR check
    stop_event = threading.Event()
    worker = threading.Thread(
        target=trade_executor.run_worker,
        args=(CONFIG_PATH, config, signal_queue, validation_queue, logger, stop_event),
        daemon=True,
    )
    worker.start()

    web_thread = threading.Thread(
        target=web.server.run_web,
        args=(CONFIG_PATH, config, signal_queue, validation_queue, logger),
        daemon=True,
    )
    web_thread.start()

    async def on_message_text(text):
        stage = "regex"
        signal = classify(text)
        if signal is None:
            stage = "claude"
            # Fetched before this message is logged below, so it never
            # includes the message being classified right now — see
            # llm_classifier.classify()'s docstring for why this is passed
            # (ticker disambiguation only, e.g. "adding back my 774p here"
            # right after a message naming SPY).
            recent = db.get_recent_raw_texts(limit=5)
            signal = await llm_classifier.classify(text, llm_client, llm_model, recent_messages=recent)
            db.increment_counter("claude_call_count")

        blocked_reason = None
        if signal.type != SignalType.NOISE and db.get_paused():
            blocked_reason = "bot paused"

        signal.db_id = log_signal(logger, signal, stage, blocked_reason)

        if signal.type != SignalType.NOISE and blocked_reason is None:
            signal_queue.put(signal)

    def on_discord_connected():
        db.set_bot_state("discord_connected", "1")

    def on_discord_message_seen():
        db.set_bot_state("discord_last_message_ts", str(time.time()))

    try:
        discord_listener.run(
            user_token=discord_cfg["user_token"],
            channel_id=discord_cfg["channel_id"],
            casey_user_id=discord_cfg["casey_user_id"],
            on_message_text=on_message_text,
            on_connected=on_discord_connected,
            on_message_seen=on_discord_message_seen,
        )
    finally:
        stop_event.set()


if __name__ == "__main__":
    sys.exit(main())
