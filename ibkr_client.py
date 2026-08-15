"""
IBKR connection, contract selection, and option order placement via
ib_async. Contract selection (strike/expiry ranking in strike_selection.py)
and pricing (option_pricing.py) are pure and unit-tested on their own; this
module is the IB-round-trip glue around them. Streaming-ticker cache,
chain/price lookup, and connect/order flow are ported from
../IBKRStocks/ibkr_trading_lib.py and ibkr_order_placer.py, which already
proved this out against real fills. options_type there is renamed direction
here to match signal_classifier.Signal.direction ('CALL'/'PUT').
"""

import logging
import time
from datetime import date, datetime

from ib_async import LimitOrder, MarketOrder, Option, Stock, StopOrder

import db
from option_pricing import is_nan, limit_price_from_quote, round_to_tick
from strike_selection import pick_expiry, rank_strike_pool

RECONNECT_DELAY_SECS = 5
OPTION_CHAIN_CACHE_TTL_SECS = 3600

# Same named logger alerting.build_logger() configures once at startup —
# logging.getLogger() returns that same configured instance from anywhere
# in the process, so this module's messages land in casey_bot.log (and the
# console) without needing a logger passed into every function here. Before
# this, every line below was a bare print() — visible on a live terminal
# only, never persisted, which is exactly why a submitted order's fate was
# unrecoverable after the fact.
_logger = logging.getLogger("casey_bot")


def connect_ibkr(ib, host, port, client_id):
    """Retries indefinitely rather than giving up — a dropped connection (or
    Gateway not up yet) shouldn't kill the process."""
    while True:
        try:
            ib.connect(host, port, clientId=client_id, timeout=10)
            _logger.info(f"[ibkr_client] Connected to {host}:{port} (clientId={client_id}).")
            return
        except Exception as e:
            _logger.warning(f"[ibkr_client] Could not connect to TWS/Gateway at "
                             f"{host}:{port}: {e} — retrying in {RECONNECT_DELAY_SECS}s")
            time.sleep(RECONNECT_DELAY_SECS)


# ── streaming ticker cache ──────────────────────────────────────────────────
# conId -> live-updating Ticker from ib.reqMktData, so reading a price after
# the initial subscribe is a memory read, not a round trip.
_stream_tickers = {}


def ensure_streaming(ib, contract, generic_ticks=""):
    if not contract.conId:
        return
    already = contract.conId in _stream_tickers
    if already and not generic_ticks:
        return
    _stream_tickers[contract.conId] = ib.reqMktData(contract, generic_ticks, False, False)


def stop_streaming(ib, con_id):
    tkr = _stream_tickers.pop(con_id, None)
    if tkr is not None:
        try:
            ib.cancelMktData(tkr.contract)
        except Exception:
            pass


def clear_streaming_cache():
    """Must be called after an IBKR reconnect: subscriptions don't survive
    it, and the orphaned Ticker objects would otherwise serve stale prices."""
    _stream_tickers.clear()


def ensure_connected(ib, host, port, client_id):
    """Call on every idle tick of run_worker's loop: reconnects to
    TWS/Gateway if the connection has dropped since the last check, and
    redoes the connection-scoped setup that doesn't survive a reconnect —
    clear_streaming_cache() (see its docstring) and re-subscribing to
    position updates via reqPositions(), same as the initial connect in
    run_worker. A no-op when still connected (ib.isConnected() is a cheap
    local flag check, not a round trip), so it's fine to call unconditionally
    every ~1s rather than only reacting to a disconnect event — reacting
    from inside an ib_async event callback would mean driving the event
    loop (via connect()'s blocking call) from a callback already running on
    that same loop, which ib_async doesn't support (see _on_commission_report's
    docstring for the same reentrancy concern elsewhere in this module).

    track_daily_pnl(ib)'s commissionReportEvent/positionEvent subscriptions
    are NOT redone here: those are plain Python callbacks registered on the
    IB object itself, not on the TWS connection, so they survive a
    reconnect — re-subscribing them here would double-fire every commission
    report and silently double-count P&L."""
    if ib.isConnected():
        return
    _logger.warning("[ibkr_client] IBKR connection lost — reconnecting...")
    connect_ibkr(ib, host, port, client_id)
    clear_streaming_cache()
    try:
        ib.reqPositions()
    except Exception:
        _logger.exception("[ibkr_client] reqPositions failed after reconnect (non-fatal)")
    _logger.info("[ibkr_client] IBKR reconnected.")


# ── underlying price / option chain / contract selection ────────────────────
_stock_cache = {}


def get_underlying_price(ib, ticker):
    """Returns (qualified Stock contract, last/midpoint price). price is
    None when the ticker doesn't qualify as a real symbol at IBKR
    (stock.conId stays unset) — a snapshot request for an unqualified
    contract isn't just unhelpful, ib_async raises trying to hash it
    ("can't be hashed because no 'conId' value exists"), so this must
    return before ever reaching _snapshot for that case rather than let
    callers like validate_ticker's own not-a-recognized-symbol check get
    preempted by that raw exception."""
    stock = _stock_cache.get(ticker)
    if stock is None:
        stock = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(stock)
        if stock.conId:
            _stock_cache[ticker] = stock
    if not stock.conId:
        return stock, None
    tkr = _snapshot(ib, stock)
    price = tkr.marketPrice()
    if is_nan(price) or price <= 0:
        price = tkr.close
    return stock, price


# ticker -> (OptionChain, cached_at). reqSecDefOptParams is the slowest
# single call in contract selection and returns the same strikes/expirations
# for a trading class all day, so caching avoids paying that round trip on
# every signal.
_option_chain_cache = {}


def get_option_chain(ib, ticker):
    """The (cached) reqSecDefOptParams chain for this ticker's SMART trading
    class — expirations + valid strikes. Returns None when IBKR reports no
    chain (or the underlying can't be qualified)."""
    now = time.monotonic()
    cached = _option_chain_cache.get(ticker)
    if cached is not None and (now - cached[1]) < OPTION_CHAIN_CACHE_TTL_SECS:
        return cached[0]

    stock, _price = get_underlying_price(ib, ticker)
    if not stock.conId:
        return None
    chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
    chain = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == ticker), None)
    if chain is None:
        chain = next((c for c in chains if c.exchange == "SMART"), None) or (chains[0] if chains else None)
        if chain is not None:
            _logger.warning(f"[ibkr_client] no SMART chain with tradingClass={ticker}; "
                             f"falling back to tradingClass={chain.tradingClass} — strikes/expirations may "
                             f"not match what you expect for {ticker}")
    if chain is not None:
        _option_chain_cache[ticker] = (chain, now)
    return chain


def validate_ticker(ib, ticker):
    """Used by the web UI's "+ add ticker" flow before a user-typed symbol
    is added to risk.allowed_tickers. Deliberately requires a real, listed
    options chain, not just a valid stock symbol — this bot only ever
    trades options, so a ticker that qualifies as a stock but has nothing
    listed against it (illiquid/foreign names) would just sit in
    allowed_tickers and silently fail the first time an ENTRY tried to use
    it. Returns (valid: bool, reason: str) — reason is empty when valid."""
    ticker = ticker.strip().upper()
    if not ticker or not ticker.isalpha() or len(ticker) > 5:
        return False, f"{ticker!r} doesn't look like a stock ticker"

    stock, _price = get_underlying_price(ib, ticker)
    if not stock.conId:
        return False, f"{ticker} isn't a recognized stock symbol at IBKR"

    chain = get_option_chain(ib, ticker)
    if chain is None:
        return False, f"{ticker} is a valid stock but has no listed options at IBKR"

    return True, ""


def select_contract_params(ib, ticker, direction, strike_selection, expiry_selection="nearest"):
    """Auto-derive (expiry, ranked candidate strikes) from config's
    strike_offset/expiry_selection. Returns the ranked pool (a list for
    find_option_contract to walk, not a single value — the preferred strike
    sometimes doesn't exist for this exact expiry), or (None, None) /
    (expiry, None) when derivation fails."""
    right = "C" if direction == "CALL" else "P"

    stock, price = get_underlying_price(ib, ticker)
    if not price or price <= 0:
        _logger.info(f"[ibkr_client] Could not get a price for {ticker}")
        return None, None

    chain = get_option_chain(ib, ticker)
    if chain is None:
        _logger.info(f"[ibkr_client] No option chain found for {ticker}")
        return None, None

    target_expiry = pick_expiry(chain.expirations, datetime.now().date(), expiry_selection)
    if target_expiry is None:
        _logger.info(f"[ibkr_client] No future expiries for {ticker}")
        return None, None

    pool = rank_strike_pool(price, chain.strikes, right, strike_selection)
    if not pool:
        _logger.info(f"[ibkr_client] No usable strikes found for {ticker}")
        return target_expiry, None

    return target_expiry, pool


def get_open_option_positions(ib):
    """All currently-held option positions at IBKR as (contract, qty,
    avg_premium) with nonzero quantity — the live, broker-truth account
    state, never our own bookkeeping, so a stray TRIM/EXIT for a ticker with
    nothing open can't be acted on by mistake (e.g. a QQQ 'I'm out' firing
    while only SPY is actually held). ib.sleep() first pumps the event loop
    so a just-arrived position update (from a fill moments ago) is
    reflected — a bare ib.positions() read can otherwise serve one tick
    stale, per ../IBKRStocks/manual_trade_service.py's _refresh_live_positions.
    avg_premium is the per-contract entry price (used to protect trim
    runners with a stop at breakeven): IBKR's avgCost is multiplier-scaled,
    same convention as ../IBKRStocks/manual_trade_service.py:604-605."""
    ib.sleep(0.5)
    result = []
    for pos in ib.positions():
        if pos.contract.secType != "OPT" or pos.position == 0:
            continue
        if not pos.contract.exchange:
            # IBKR's position reports come back with exchange blank (a
            # position isn't tied to a routing venue). Passing that straight
            # to placeOrder() gets rejected with error 321 "Missing order
            # exchange" — every entry contract gets "SMART" from
            # find_option_contract, so match that here for exit/trim/stop.
            pos.contract.exchange = "SMART"
        multiplier = float(pos.contract.multiplier or 100)
        avg_premium = pos.avgCost / multiplier if pos.avgCost else None
        result.append((pos.contract, pos.position, avg_premium))
    return result


def get_open_option_position(ib, ticker):
    """The currently-held option contract + qty + avg_premium for this
    ticker, or (None, 0, None) if nothing is open for it."""
    for contract, qty, avg_premium in get_open_option_positions(ib):
        if contract.symbol == ticker:
            return contract, qty, avg_premium
    return None, 0, None


# ── daily realized-P&L tracking (risk.max_daily_losing_trades /
# risk.daily_loss_limit) ─────────────────────────────────────────────────
# In-memory only — an accepted simplification pre-live-trading, same as
# everywhere else in this bot; there's no persistent trade-history ledger
# for this specific tally (see trade_executor.py's docstring). Reset both
# on a process restart AND at every calendar-day boundary (_stats_day /
# _reset_daily_stats_if_new_day below) — a restart alone isn't enough,
# since this bot is meant to run for days at a stretch and "daily" limits
# that only ever reset on restart would permanently wedge new
# ENTRY/ADD signals the day after a bad day.
#
# _open_conid_pnl/_open_conid_contract/_last_known_qty are NOT part of that
# daily reset — they track P&L accrual for currently-open positions until
# each goes flat, independent of calendar day (a position held overnight
# across a day boundary still needs its pre-boundary fills counted once it
# finally closes).
#
# A "losing trade" is counted once per ticker, when its position closes
# back to flat, netting every reducing fill on that contract together
# (each trim plus the final exit) — not once per fill. IBKR reports
# realizedPNL per execution, so a single position trimmed twice then
# stopped out would otherwise burn through the daily count by itself.
#
# Accumulation (commissionReportEvent, has the P&L) and the now-flat
# check (positionEvent, has the live quantity) are two separate
# subscriptions rather than one, because IBKR doesn't guarantee which of
# the two arrives first for a given fill (both orderings are observed in
# practice) — _finalize_if_flat is called from both handlers and only
# actually arms a write once both "flat" and "has accumulated P&L" are
# true, so it's correct regardless of arrival order. Deliberately doesn't
# ask ib.positions() inside the commissionReport callback instead — that
# would mean pumping the event loop for a fresh read from inside a
# callback already running on that same loop, which ib_async doesn't
# support (no reentrant event loop).
#
# The write itself is debounced (FINALIZE_DEBOUNCE_SECS / _pending_finalize
# / flush_pending_round_trips below), not immediate: IBKR doesn't guarantee
# every fill of a close reports its commission before the position update
# goes flat, and a close can span more than one order (e.g. a trim's sell
# plus its protective stop closing out the remainder within the same
# second) — writing on the very first "flat + some PnL" sighting split a
# real 24-lot close (10-lot trim + 14-lot stop, one second apart) into two
# separate round_trip rows in production instead of one netted row, and
# double-counted it against max_daily_losing_trades in the process.
_daily_realized_pnl = 0.0
_daily_losing_trades = 0
_stats_day = date.today()
_open_conid_pnl = {}   # conId -> realized P&L accumulated so far today while still open
_open_conid_contract = {}  # conId -> Contract, stashed for the round_trips row _write_round_trip writes
_last_known_qty = {}   # conId -> most recent position size IBKR has reported

# how long to wait, after a conId is first seen flat with accumulated P&L,
# before actually writing its round trip — long enough to absorb the
# observed ~1s gap between the last two fills of one multi-order close,
# short enough that daily_risk_stats() (consulted before every ENTRY/ADD)
# is never stale for more than about one flush_pending_round_trips() tick
# plus this window. Any further reducing fill for the same conId while a
# write is pending re-accumulates into _open_conid_pnl and pushes the write
# back out (see _finalize_if_flat).
FINALIZE_DEBOUNCE_SECS = 3.0

# conId -> wall-clock time its round trip is safe to write. Deliberately
# synchronous/asyncio-free (a plain dict flushed by flush_pending_round_trips,
# called periodically from run_worker's poll loop) rather than
# loop.call_later, so this stays testable with an injected `now` like every
# other time-windowed piece of state in this bot (PENDING_WINDOW_SECS,
# reconnect.retry_timeout_mins, ...).
_pending_finalize = {}


def _reset_daily_stats_if_new_day():
    """Called from every read and write of _daily_realized_pnl/
    _daily_losing_trades, so whichever happens first after midnight — a
    fill finalizing, or a signal checking the halt — sees a fresh day
    rather than yesterday's leftover tally. Uses the local machine's
    calendar day (date.today()), same convention as the web UI's
    day-grouping in web/server.py's _week_pnl/_kpis, not the exchange's
    trading-day boundary."""
    global _stats_day, _daily_realized_pnl, _daily_losing_trades
    today = date.today()
    if today != _stats_day:
        _daily_realized_pnl = 0.0
        _daily_losing_trades = 0
        _stats_day = today


def track_daily_pnl(ib):
    """Call once at startup. Feeds risk.max_daily_losing_trades and
    risk.daily_loss_limit — see daily_risk_stats()."""
    ib.commissionReportEvent += _on_commission_report
    ib.positionEvent += _on_position_update


def _write_round_trip(con_id):
    """Actually persists con_id's accumulated P&L as a closed round trip —
    to db.round_trips for the web UI's History screen, and to the
    in-memory daily-loser tally. mode is always "LIVE": a DRY-run order
    (require_confirmation) is never actually submitted, so it can never
    fill or go flat. Called once per close, from flush_pending_round_trips
    once its debounce window has elapsed, or immediately from
    _on_position_update if the conId reopens before that window is up."""
    global _daily_losing_trades
    _pending_finalize.pop(con_id, None)
    if con_id not in _open_conid_pnl:
        return
    pnl = _open_conid_pnl.pop(con_id)
    if pnl < 0:
        _daily_losing_trades += 1
    contract = _open_conid_contract.pop(con_id, None)
    if contract is not None:
        contract_label = (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                           f"{contract.strike}{contract.right}")
        try:
            db.insert_round_trip(contract.symbol, contract_label, pnl, mode="LIVE")
        except Exception:
            _logger.exception(f"Failed to record round trip for {contract_label} (non-fatal)")


def _finalize_if_flat(con_id, now):
    """Once a contract is both flat (qty 0) and has accumulated realized
    P&L, (re)arm a debounced write FINALIZE_DEBOUNCE_SECS out rather than
    writing immediately — see FINALIZE_DEBOUNCE_SECS for why. Safe to call
    repeatedly while still flat: each call just pushes the write further
    out, which is exactly what a trailing fill for the same close should
    do."""
    if _last_known_qty.get(con_id) == 0 and con_id in _open_conid_pnl:
        _reset_daily_stats_if_new_day()
        _pending_finalize[con_id] = now + FINALIZE_DEBOUNCE_SECS


def flush_pending_round_trips(now=None):
    """Writes any round trip whose debounce window has elapsed. Call
    periodically from run_worker's poll loop (idle-tick branch) so
    daily_risk_stats() — consulted before every ENTRY/ADD — is never stale
    for more than about one tick plus FINALIZE_DEBOUNCE_SECS.

    now: injectable for tests; production callers omit it (defaults to
    time.time())."""
    if now is None:
        now = time.time()
    for con_id, ready_at in list(_pending_finalize.items()):
        if now >= ready_at:
            _write_round_trip(con_id)


def _on_commission_report(trade, fill, report):
    global _daily_realized_pnl
    realized = report.realizedPNL or 0.0
    if realized == 0.0:
        return  # opening fills always report 0.0 — nothing to accumulate
    now = time.time()
    _reset_daily_stats_if_new_day()
    con_id = trade.contract.conId
    _daily_realized_pnl += realized
    _open_conid_pnl[con_id] = _open_conid_pnl.get(con_id, 0.0) + realized
    _open_conid_contract[con_id] = trade.contract
    _finalize_if_flat(con_id, now)


def _on_position_update(position):
    con_id = position.contract.conId
    now = time.time()
    if position.position != 0 and con_id in _pending_finalize:
        # reopened while a debounced finalize for a PRIOR close on this same
        # conId was still pending — flush that close now, using whatever had
        # accumulated, rather than risk a fast re-entry's fills bleeding
        # into the same accumulator as the already-closed round trip.
        _write_round_trip(con_id)
    _last_known_qty[con_id] = position.position
    _finalize_if_flat(con_id, now)


def daily_risk_stats():
    """(realized_pnl, losing_trades) accumulated so far today — see the
    reset caveats in the block comment above (process restart AND calendar
    day, not just restart)."""
    _reset_daily_stats_if_new_day()
    return _daily_realized_pnl, _daily_losing_trades


# (symbol, expiry, strike, right) -> qualified Option, or None for a strike
# IBKR rejected (no such contract for that expiry). conIds are stable and the
# expiry is part of the key, so entries never go stale within a process's
# lifetime.
_qualified_options = {}
_fallback_logged = set()  # dedupes the "falling back" log line per key


def find_option_contract(ib, ticker, direction, expiry, strikes):
    """Resolve and qualify an options contract, trying each strike in
    `strikes` (preference order) until one qualifies at IBKR — chain.strikes
    isn't guaranteed valid for this specific expiry, so the preferred strike
    sometimes doesn't exist and the next one does.
    expiry: 'YYYYMMDD'. strikes: a single float, or an ordered list of
    fallback candidates (as returned by select_contract_params)."""
    right = "C" if direction == "CALL" else "P"
    candidates = strikes if isinstance(strikes, (list, tuple)) else [strikes]
    for i, strike in enumerate(candidates):
        key = (ticker, expiry, strike, right)
        if key in _qualified_options:
            contract = _qualified_options[key]
            if contract is None:
                continue  # known invalid for this expiry — skip, no round trip
        else:
            contract = Option(ticker, expiry, strike, right, "SMART", currency="USD")
            ib.qualifyContracts(contract)
            _qualified_options[key] = contract if contract.conId else None
            if not contract.conId:
                continue
        if i > 0 and key not in _fallback_logged:
            _fallback_logged.add(key)
            _logger.info(f"[ibkr_client] Preferred strike {candidates[0]} has no such "
                         f"contract at IBKR for {ticker} {expiry} — falling back to {strike}")
        return contract
    raise ValueError(f"No such contract at IBKR: {ticker} {expiry} {candidates[-1]}{right}")


def _snapshot(ib, contract, use_stream=True):
    """A live streaming Ticker if subscribed and usable, else a reqTickers
    snapshot, falling back to delayed data if live isn't subscribed (common
    on paper accounts without a market data subscription)."""
    if use_stream:
        tkr = _stream_tickers.get(contract.conId)
        if tkr is not None and not (is_nan(tkr.marketPrice()) and is_nan(tkr.last) and is_nan(tkr.close)):
            return tkr
    [tkr] = ib.reqTickers(contract)
    if is_nan(tkr.marketPrice()) and is_nan(tkr.last) and is_nan(tkr.close):
        ib.reqMarketDataType(3)  # delayed
        [tkr] = ib.reqTickers(contract)
    return tkr


def _best_price(tkr):
    for val in (tkr.last, tkr.marketPrice(), tkr.close):
        if val is not None and not is_nan(val):
            return float(val)
    return None


def get_last_price(ib, contract):
    """Best-effort last-traded price for display only (the web UI's
    positions snapshot, see trade_executor._snapshot_state) — never a valid
    limit price; compute_limit_price below is what order placement uses."""
    return _best_price(_snapshot(ib, contract))


def get_entry_price_snapshot(ib, contract):
    """Same best-effort last-traded price as get_last_price, but also
    returns the underlying Ticker snapshot itself — for handle_entry's
    dynamic (capital-per-trade) contract sizing, which needs a real price
    *before* qty is known and thus before place_order's own price lookup.
    Returning the Ticker alongside lets that same snapshot be passed into
    place_order(tkr=...) afterward so pricing the order doesn't poll IBKR a
    second time for data already in hand. Returns (price_or_None, tkr)."""
    tkr = _snapshot(ib, contract)
    return _best_price(tkr), tkr


def compute_limit_price(ib, contract, price_type, tkr=None):
    if tkr is None:
        tkr = _snapshot(ib, contract)
    price = limit_price_from_quote(tkr.bid, tkr.ask, tkr.last, price_type)
    if price <= 0 and tkr is _stream_tickers.get(contract.conId):
        # Streaming ticker had SOME data but not the field this price_type
        # needs (e.g. only a close tick has arrived) — force a real snapshot.
        tkr = _snapshot(ib, contract, use_stream=False)
        price = limit_price_from_quote(tkr.bid, tkr.ask, tkr.last, price_type)
    if price <= 0:
        return 0.0
    return round_to_tick(price)


def _log_order_status(trade):
    """Subscribed to every order's statusEvent so a fill, rejection, or an
    order stuck on Submitted/Inactive shows up as its own clear log line —
    ib.placeOrder() returning a Trade only means IBKR received the request,
    never that it actually filled (or even got accepted)."""
    contract = trade.contract
    status = trade.orderStatus
    _logger.info(f"[ibkr_client] Order status: id={trade.order.orderId} "
                 f"{trade.order.action} x{trade.order.totalQuantity} {contract.symbol} "
                 f"{contract.lastTradeDateOrContractMonth} {contract.strike}{contract.right} "
                 f"-> {status.status} (filled={status.filled} remaining={status.remaining} "
                 f"avgFillPrice={status.avgFillPrice})")


def place_order(ib, contract, action, qty, price_type, account="", tkr=None):
    """action: BUY | SELL. price_type: MARKET | MIDPOINT | BID | ASK | LAST.
    account: required on any login with more than one managed account (IBKR
    error 435); empty is fine on a single-account login. tkr: an already-
    fetched Ticker snapshot for this contract (e.g. from
    get_entry_price_snapshot, used by handle_entry's dynamic contract
    sizing) to price this order from instead of polling IBKR again."""
    price_type = price_type.upper()
    if price_type == "MARKET":
        order = MarketOrder(action, qty)
        _logger.info(f"[ibkr_client] Submitting MARKET {action} x{qty} "
                     f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                     f"{contract.strike}{contract.right} (conId={contract.conId})")
    else:
        limit_price = compute_limit_price(ib, contract, price_type, tkr=tkr)
        if limit_price <= 0:
            raise ValueError("Could not determine a valid limit price")
        order = LimitOrder(action, qty, limit_price)
        _logger.info(f"[ibkr_client] Submitting LIMIT {action} x{qty} "
                     f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                     f"{contract.strike}{contract.right} (conId={contract.conId}) @ {limit_price} "
                     f"[{price_type}]")

    order.tif = "DAY"
    if account:
        order.account = account
    trade = ib.placeOrder(contract, order)
    trade.statusEvent += _log_order_status
    _logger.info(f"[ibkr_client] Order submitted: ibkr_order_id={trade.order.orderId}")
    return trade


def place_stop_order(ib, contract, action, qty, stop_price, account=""):
    """Protective stop for runner contracts left over after a trim.
    stop_price: trigger price as a per-contract premium (same convention as
    place_order's limit_price), tick-rounded via round_to_tick. TIF DAY,
    matching every other order type this bot places."""
    order = StopOrder(action, qty, round_to_tick(stop_price))
    order.tif = "DAY"
    if account:
        order.account = account
    _logger.info(f"[ibkr_client] Submitting STOP {action} x{qty} "
                 f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                 f"{contract.strike}{contract.right} (conId={contract.conId}) @ {order.auxPrice}")
    trade = ib.placeOrder(contract, order)
    trade.statusEvent += _log_order_status
    _logger.info(f"[ibkr_client] Stop order submitted: ibkr_order_id={trade.order.orderId}")
    return trade


def _open_stop_trades(ib, contract):
    return [
        trade for trade in ib.openTrades()
        if trade.contract.conId == contract.conId
        and trade.order.orderType == "STP"
        and trade.order.action == "SELL"
    ]


def cancel_open_stop_orders(ib, contract):
    """Cancels any open protective STP SELL order(s) IBKR is holding for this
    exact contract (conId) — used before replacing a stop with one sized to
    a new remaining quantity, and when a position is fully closed. Returns
    the number of orders cancelled."""
    cancelled = 0
    for trade in _open_stop_trades(ib, contract):
        ib.cancelOrder(trade.order)
        cancelled += 1
    return cancelled


def count_open_stop_orders(ib, contract):
    """Read-only count of open protective STP SELL order(s) for this exact
    contract — lets a require_confirmation dry-run describe what a cancel
    would do without actually cancelling anything."""
    return len(_open_stop_trades(ib, contract))
