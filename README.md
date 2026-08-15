# thecaseybot
Reads Casey's Discord, places IBKR options orders accordingly

## What it does

- Watches a Discord channel/author for trading calls and classifies each message
  as ENTRY / EXIT / TRIM / ADD / NOISE (regex first, Claude as fallback for
  anything ambiguous).
- Turns a classified signal into a risk-gated Interactive Brokers options order.
- Serves a local control UI, **Casey Bridge**, at `http://127.0.0.1:8787` — live
  positions, signal feed, order history, and a Settings screen.

## Requirements

- Python 3.9+
- [IBKR TWS or IB Gateway](https://www.interactivebrokers.com/en/trading/tws.php) —
  paper account recommended to start
- A Discord personal account token (self-bot mode — see
  [Getting your Discord token](#getting-your-discord-token))
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
git clone https://github.com/rajricardo/thecaseybot.git
cd thecaseybot
cp config.example.yaml config.yaml
```

Fill in `config.yaml`:
- `discord.*` — see [Getting your Discord token](#getting-your-discord-token)
- `llm.api_key` — your Anthropic API key
- `ibkr.*` — see [IBKR TWS/Gateway API setup](#ibkr-twsgateway-api-setup)
- `risk.*` — see the [config reference](#risk-config-reference) below; also
  editable live from the Settings screen once running

Run it:

```bash
./run.sh
```

(creates the venv, installs dependencies, and starts the bot — or do it by hand
with `python3 -m venv venv && venv/bin/pip install -r requirements.txt &&
venv/bin/python bot.py`.) Then open `http://127.0.0.1:8787`.

**Start with `risk.require_confirmation: true`** — signals get logged but no
order is ever submitted. Flip it to `false` only once you've watched it behave
correctly against a paper account.

### Getting your Discord token

1. In Discord, enable **Developer Mode**: User Settings → Advanced → Developer
   Mode. This unlocks a "Copy ID" option when you right-click things.
2. Right-click the channel to watch → **Copy Channel ID** → `discord.channel_id`.
   Right-click Casey's name → **Copy User ID** → `discord.casey_user_id`.
3. For the token: open Discord in a browser, open DevTools
   (`Cmd+Option+I` / `F12`) → **Network** tab, click into any channel so a
   request fires, open any request to `discord.com/api/...`, and copy the
   `authorization` value from its request headers → `discord.user_token`.

This is your own account's session token, not a bot token — automating a
personal account is against Discord's ToS and risks the account. Treat the
token like a password: never commit it, never paste it into a notes file.

### IBKR TWS/Gateway API setup

1. Open TWS or [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)
   (lighter-weight, headless) and log in.
2. **Configuration → API → Settings**, then:
   - Check **Enable ActiveX and Socket Clients**
   - Set **Socket port** to match `ibkr.port` (4001 paper / 4002 live Gateway,
     7497/7496 paper/live TWS)
   - **Uncheck "Read-Only API"** — otherwise every order gets silently rejected
   - Add `127.0.0.1` to **Trusted IP Addresses**
   - Check **"Bypass Order Precautions for API Orders"** — without this, TWS
     pops up a confirmation dialog with nobody there to click it, and the
     order just hangs. `risk.require_confirmation` in `config.yaml` is the
     bot's own separate gate, unaffected by this.
3. Make sure `ibkr.client_id` isn't already used by another connection.

## `risk` config reference

| Key | Meaning |
|---|---|
| `allowed_tickers` | Only ENTRY signals for these tickers are acted on |
| `sizing_mode` | `fixed` — every entry uses `max_contracts_per_trade` contracts. `dynamic` — contracts are derived from `capital_per_trade` and the contract's live LAST price: `floor(capital_per_trade / (LAST price * 100))` |
| `max_contracts_per_trade` | Contracts per entry when `sizing_mode` is `fixed` |
| `capital_per_trade` | Dollars to deploy per entry when `sizing_mode` is `dynamic` |
| `max_concurrent_positions` | Blocks new entries once this many distinct tickers are open |
| `max_daily_losing_trades` | Blocks new ENTRY/ADD once this many trades close as losers today (TRIM/EXIT are never blocked) |
| `daily_loss_limit` | Same, but keyed off today's realized P&L in dollars instead of a trade count; `null` disables it |
| `strike_offset` | `<rank>ITM` \| `<rank>OTM`, e.g. `1ITM`, `2OTM` — there's no ATM, use `1OTM` for closest-to-money |
| `expiry_selection` | `nearest` (0DTE if available) or `weeklies` (next Friday) |
| `price_type` | `MARKET` \| `MIDPOINT` \| `BID` \| `ASK` \| `LAST` \| `AUTO`. `AUTO` submits `MIDPOINT` before 9:45am ET and switches to `MARKET` from 9:45am ET on, since IBKR doesn't accept MARKET orders on options right at the open |
| `require_confirmation` | `true` = log the intended order but never submit it. `false` = actually place it |
| `trim_pct` | % of the *currently held* quantity sold on every TRIM signal (applied fresh each time) |
| `auto_submit_stop_loss` | After a TRIM, place a protective stop on the remaining runners at the average entry price |
| `add_pct` | % of the *currently held* quantity bought on every ADD signal |

`reconnect.*` controls what happens to a signal that arrived while IBKR was
disconnected, once the connection comes back — see the comments in
`config.example.yaml`.

## Safety notes

- This bot places real brokerage orders. Test against an IBKR **paper** account
  with `require_confirmation: true` before ever pointing it at a live account.
- `config.yaml` holds real secrets and is gitignored — never commit it.
- The Casey Bridge web UI binds to `127.0.0.1` only.
