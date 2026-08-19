"""
Phase-1 alerting: structured logging of every classified signal so
classifier accuracy can be reviewed and tuned against real channel
traffic before any IBKR order is ever wired up.
"""

import json
import logging

import db
from signal_classifier import SignalType


class _QuietPortfolioUpdates(logging.Filter):
    """Drops ib_async.wrapper's updatePortfolio/position callback dumps —
    these fire repeatedly on every portfolio tick while any position is
    open and carry no actionable information (just a full contract/position
    object echoed back). Every other ib_async.* line — orderStatus,
    execDetails, commissionReport, error/warning — is left alone; those are
    exactly the ones build_logger's ib_async routing exists to preserve.

    Attached to the handlers, not to the "ib_async" logger itself: these
    messages are actually logged via the "ib_async.wrapper" child logger
    (see ib_async/wrapper.py's self._logger), and a Filter added via
    Logger.addFilter() only runs for records logged directly on that same
    logger object — it's never consulted for records a child logger
    propagates up through callHandlers(). Handlers, by contrast, see every
    record that reaches them regardless of which child logger it came from,
    so that's where this has to live to actually take effect."""

    def filter(self, record):
        if record.name != "ib_async.wrapper":
            return True
        msg = record.getMessage()
        return not (msg.startswith("updatePortfolio:") or msg.startswith("position:"))


def build_logger(log_file):
    logger = logging.getLogger("casey_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(name)s] %(message)s")
    quiet_portfolio_updates = _QuietPortfolioUpdates()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(quiet_portfolio_updates)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(quiet_portfolio_updates)
    logger.addHandler(console_handler)

    # ib_async logs every order status change, fill, and IBKR API
    # error/warning (rejections, connectivity issues, etc.) internally via
    # its own "ib_async.*" loggers (see ib_async/wrapper.py's error() and
    # orderStatus() handlers) — without this, that traffic has no handler
    # attached anywhere and is silently dropped, which is exactly why an
    # order that never actually filled at IBKR left no trace beyond our own
    # "submitted" line. Route it into the same file/console so a rejection
    # or a status stuck on Submitted/Inactive shows up right next to our
    # own TRIM/EXIT/ENTRY log lines.
    ib_logger = logging.getLogger("ib_async")
    ib_logger.setLevel(logging.INFO)
    ib_logger.handlers.clear()
    ib_logger.addHandler(file_handler)
    ib_logger.addHandler(console_handler)

    return logger


def log_signal(logger, signal, stage, blocked_reason=None):
    """Logs to casey_bot.log as before, and inserts a row into db.signals
    for the web UI's feed — stage ("regex"/"claude") and blocked_reason
    (e.g. "bot paused") come from the caller, which is the only place that
    knows which classifier resolved the message and whether it was actually
    enqueued for execution. Returns the new row's id so the caller can
    stash it on the Signal (signal.db_id) for trade_executor to later write
    its outcome back onto this same row."""
    record = {
        "type": signal.type.value,
        "ticker": signal.ticker,
        "direction": signal.direction,
        "reason": signal.reason,
        "text": signal.raw_text,
    }
    if signal.type == SignalType.NOISE:
        logger.info("NOISE %s", json.dumps(record, ensure_ascii=False))
    else:
        logger.info("SIGNAL %s", json.dumps(record, ensure_ascii=False))

    return db.insert_signal(
        signal.type.value, signal.ticker, signal.direction, signal.reason,
        signal.raw_text, stage, blocked_reason,
    )
