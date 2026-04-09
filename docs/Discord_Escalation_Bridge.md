# Discord Escalation Bridge

When the agent workflow pauses due to an escalation (`escalated.json`), a human
reply in Discord is sufficient to resume it. The system automatically writes
`resume.json` from the Discord message, so the existing pause/resume semantics
remain auditable and deterministic.

---

## How it works

```
escalated.json appears
        │
        ▼
wait_for_resume() in run.py
        │
        ├─ starts discord_escalation_bridge.py (background)
        │         │
        │         ├─ Posts escalation to #agent-escalations in arigato-mr-roboto server
        │         ├─ Creates a thread off that post (idempotent via discord_notified.json)
        │         └─ Polls for RESUME: reply
        │
        └─ polls for resume.json (every 5 s)

Authorized user replies in thread with "RESUME: Q1=..."
        │
        ▼
Bridge writes resume.json (with Discord provenance)
        │
        ▼
wait_for_resume() reads resume.json, archives escalated.json, returns resolution
        │
        ▼
Workflow continues
```

---

## Getting started

### 1. Create a Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Under **Bot**, click **Add Bot**, then copy the **Token**.
3. Under **OAuth2 → URL Generator**, select scopes: `bot`, and permissions:
   - Send Messages
   - Create Public Threads
   - Read Message History
   - View Channels
4. Open the generated URL in your browser and invite the bot to your server
   (server name: **arigato-mr-roboto**).

### 2. Set environment variables

| Variable                   | Required    | Default             | Description                                        |
| -------------------------- | ----------- | ------------------- | -------------------------------------------------- |
| `DISCORD_BOT_TOKEN`        | ✅          | —                      | Bot token from the developer portal                |
| `DISCORD_GUILD_NAME`       | No          | `arigato-mr-roboto`    | Discord server name                                |
| `DISCORD_CHANNEL_NAME`     | No          | `agent-escalations`    | Channel for escalation posts                       |
| `DISCORD_ALLOWED_USER_IDS` | Recommended | —                      | Comma-separated Discord user IDs allowed to resume |
| `DISCORD_POLL_SECONDS`     | No          | `5`                    | How often to poll for new messages                 |

```bash
export DISCORD_BOT_TOKEN="your-bot-token-here"
export DISCORD_ALLOWED_USER_IDS="123456789012345678,987654321098765432"
```

### 3. (Optional) Use a config file instead of env vars for the allowlist

Create `.claude/discord_config.json` in the repo root:

```json
{
  "allowed_user_ids": ["123456789012345678", "987654321098765432"]
}
```

---

## The RESUME: message format

Reply in the escalation thread with any of these formats:

```
RESUME: Q1=my answer to question 1, Q2=my answer to question 2
```

```
RESUME:
Q1=my answer to question 1
Q2=my answer to question 2
```

```
RESUME: free-form explanation of what the agent should do
```

The bridge parses key=value pairs for structured answers. If the format is
unrecognized, the bot will prompt you to try again. If questions remain
unanswered, the bot will ask only the missing ones.

---

## What resume.json looks like

```json
{
  "responder": "jsmith#1234",
  "timestamp": "2026-04-08T14:23:11Z",
  "answers": {
    "Q1": "Use the existing API endpoint",
    "Q2": "Target the staging environment"
  },
  "constraints": [],
  "extra_context": "RESUME: Q1=Use the existing API endpoint, Q2=Target the staging environment",
  "discord": {
    "guild_id": "1234567890",
    "channel_id": "9876543210",
    "thread_id": "1122334455",
    "message_id": "5544332211",
    "user_id": "123456789012345678",
    "permalink": "https://discord.com/channels/1234567890/1122334455/5544332211"
  }
}
```

---

## Idempotency: no duplicate posts

The bridge writes `status/discord_notified.json` after posting. If the runner
restarts while still escalated, the bridge reads that file and reattaches to the
existing thread instead of posting again.

---

## Backward compatibility

If `DISCORD_BOT_TOKEN` is not set, the bridge is not started and the workflow
falls back to the original manual behavior: create `resume.json` by hand at the
path printed in the console output.

---

## Dry-run / developer testing (no real Discord needed)

Set `DISCORD_DRY_RUN=1` to activate dry-run mode:

```bash
export DISCORD_DRY_RUN=1
python agent-runner/run.py
```

While paused, create a simulated message file:

```bash
echo "RESUME: Q1=my test answer" > agent-context/WI-XXXX/status/discord_simulated_message.txt
```

The bridge will parse it, write `resume.json`, and the workflow will resume — no
Discord server required.

---

## Security notes

- Only Discord user IDs in the `DISCORD_ALLOWED_USER_IDS` allowlist can resume the workflow.
- Messages from bots are always ignored.
- Messages that do not start with `RESUME:` are silently ignored with a polite response in the thread.
- The `resume.json` file includes full Discord provenance (server, channel, thread, message, user IDs and permalink) for auditability.
