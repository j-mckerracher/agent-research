#!/usr/bin/env python3
"""Quick smoke-test: verify the bot token works and post a test message.

Usage:
    DISCORD_BOT_TOKEN=<token> python3 test_discord_connection.py

Expects:
    Guild:   arigato-mr-roboto
    Channel: #agent-escalations
"""

from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DISCORD_API_BASE = "https://discord.com/api/v10"
GUILD_NAME = "arigato-mr-roboto"
CHANNEL_NAME = "agent-escalations"


def _req(method: str, endpoint: str, token: str, payload: dict | None = None) -> object:
    url = f"{DISCORD_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordEscalationBridge/1.0",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"  ✗ HTTP {exc.code}: {body[:300]}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN env var is not set.", file=sys.stderr)
        print("  Set it with:  export DISCORD_BOT_TOKEN=<your-bot-token>", file=sys.stderr)
        sys.exit(1)

    # 1 — Who am I?
    print("1. Verifying bot identity...")
    me = _req("GET", "/users/@me", token)
    assert isinstance(me, dict)
    print(f"   Bot: {me.get('username')}#{me.get('discriminator')}  (id={me.get('id')})")

    # 2 — Find the guild
    print(f"2. Looking for guild '{GUILD_NAME}'...")
    guilds = _req("GET", "/users/@me/guilds", token)
    assert isinstance(guilds, list)
    guild = next((g for g in guilds if g.get("name") == GUILD_NAME), None)
    if not guild:
        names = [g.get("name") for g in guilds]
        print(f"   ✗ Guild not found.  Bot is in: {names}", file=sys.stderr)
        sys.exit(1)
    guild_id = guild["id"]
    print(f"   Guild ID: {guild_id}")

    # 3 — Find the channel
    print(f"3. Looking for channel '#{CHANNEL_NAME}'...")
    channels = _req("GET", f"/guilds/{guild_id}/channels", token)
    assert isinstance(channels, list)
    ch = next(
        (c for c in channels if c.get("name") == CHANNEL_NAME and c.get("type") in (0, 5)),
        None,
    )
    if not ch:
        available = [c.get("name") for c in channels if c.get("type") in (0, 5)]
        print(f"   ✗ Channel not found.  Available: {available}", file=sys.stderr)
        sys.exit(1)
    channel_id = ch["id"]
    print(f"   Channel ID: {channel_id}")

    # 4 — Post a test message
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        f"🤖 **Discord Escalation Bridge — connection test**\n"
        f"Timestamp: `{ts}`\n"
        f"Guild: `{GUILD_NAME}` · Channel: `#{CHANNEL_NAME}`\n"
        f"✅ Bot is connected and ready."
    )
    print("4. Posting test message...")
    msg = _req("POST", f"/channels/{channel_id}/messages", token, {"content": content})
    assert isinstance(msg, dict)
    msg_id = msg.get("id")
    permalink = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"
    print(f"   Message ID: {msg_id}")
    print(f"   Permalink:  {permalink}")
    print("\n✅ All checks passed — bridge is ready to use.")


if __name__ == "__main__":
    main()

