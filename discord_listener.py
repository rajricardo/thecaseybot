"""
Phase-1 Discord listener: reads messages from the configured channel/author
and hands raw text to on_message_text. No IBKR calls happen here or anywhere
downstream in Phase 1 — this is read + classify + log only.

on_message_text is awaited (must be an async function): the regex path in
signal_classifier.classify() is effectively instant, but anything it can't
resolve now falls through to llm_classifier.classify(), a real network call
to Claude (~1-2s). Calling that synchronously from here would block this
same asyncio event loop that's also carrying the Discord connection's
heartbeat — awaiting it instead lets the loop keep servicing the connection
while the call is in flight.

Self-bot mode: logs in with a personal Discord account's session token
(not a Developer Portal bot token) to read a server the account is only a
member of, no admin/bot-invite rights needed. This is against Discord's
Terms of Service (automating a user account) and risks the account being
actioned. Uses discord.py-self (not discord.py) since it mimics a real
client's connection fingerprint; running a plain discord.py Client with a
user token will get rejected outright.
"""

import logging

import discord


def run(user_token, channel_id, casey_user_id, on_message_text, on_connected=None,
        on_message_seen=None):
    """on_connected/on_message_seen are optional sync callbacks (not awaited —
    keep them fast) the caller can use to observe connection health without
    this module needing to know anything about how that's tracked (bot.py
    wires them to db.py's bot_state for the web UI's health indicator)."""
    # discord.http has exactly two INFO-level log calls in the whole module
    # (the user-agent and TLS-fingerprint-target echoes printed once per
    # connect) — everything else it logs (rate limits, Cloudflare throttling,
    # retries) is WARNING or higher, so raising just this child logger's
    # level is a complete fix with no risk of silencing a real rate-limit
    # warning. Must be set before client.run() below, since that calls
    # discord.utils.setup_logging() internally, which sets the parent
    # "discord" logger's level/handler but never touches this child's level.
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    client = discord.Client()

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}. Watching channel {channel_id} for user {casey_user_id}.")
        if on_connected:
            on_connected()

    @client.event
    async def on_message(message):
        if message.channel.id != channel_id:
            return
        if message.author.id != casey_user_id:
            return
        if not message.content:
            return
        if on_message_seen:
            on_message_seen()
        await on_message_text(message.content)

    client.run(user_token)
