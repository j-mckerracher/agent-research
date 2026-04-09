# Discord Escalation & Trigger System — Setup Guide

This guide covers two features that share the same Discord bot:

- **Escalation Bridge** — agents pause and post to Discord when they need human input; you reply to resume them
- **Trigger Listener** — you post a `RUN:` command in Discord to kick off a full agent workflow remotely

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Step 1 — Create a Discord Bot](#step-1--create-a-discord-bot)
4. [Step 2 — Set Up the Discord Server Channels](#step-2--set-up-the-discord-server-channels)
5. [Step 3 — Add the Bot to Your Server](#step-3--add-the-bot-to-your-server)
6. [Step 4 — Configure the Repository](#step-4--configure-the-repository)
7. [Step 5 — Set the Bot Token](#step-5--set-the-bot-token)
8. [Step 6 — Test the Connection](#step-6--test-the-connection)
9. [Step 7 — Start the Trigger Listener](#step-7--start-the-trigger-listener)
10. [Step 8 — Verify the Escalation Hook](#step-8--verify-the-escalation-hook)
11. [Triggering a Workflow from Discord](#triggering-a-workflow-from-discord)
12. [Responding to Escalations in Discord](#responding-to-escalations-in-discord)
13. [How Agents Raise Escalations](#how-agents-raise-escalations)
14. [File Reference](#file-reference)
15. [Troubleshooting](#troubleshooting)

---

## How It Works

### Escalation Flow (agent → Discord → agent)

```
Agent writes  {CHANGE-ID}/status/escalated.json
        ↓
Claude Code fires Stop / SubagentStop event
→ pause_on_escalate.py hook runs
→ Writes paused.json
→ Spawns discord_escalation_bridge.py in background
→ Exits code 2  (blocks the session from stopping)
        ↓
Discord bridge posts to #agent-escalations + creates a thread
→ Polls thread every 5s for a RESUME: reply
        ↓
Human replies:  RESUME: Q1=my answer, Q2=another answer
        ↓
Bridge writes {CHANGE-ID}/status/resume.json  →  exits
        ↓
Hook detects resume.json on next fire
→ Archives escalated.json, deletes markers
→ Exits code 0  (session continues)
→ Agent reads answers from resume.json
```

### Trigger Flow (Discord → agent)

```
Human posts in #trigger-agents:   RUN: WI-4461550
        ↓
discord_trigger_listener.py (persistent process) picks it up
→ Creates thread  run-WI-4461550  in #trigger-agents
→ Posts: 🤖 Workflow triggered — starting…
        ↓
Spawns:  python3 agent-runner/run_headless.py --change-id WI-4461550
→ Streams stdout/stderr to the thread in batched code blocks
        ↓
When run completes, posts a summary table:

  ✅ Workflow Complete — WI-4461550
  Elapsed: 4m 32s  |  Status: PASS
  | Stage             | Result | Attempts |
  | intake            | ✅     | 1        |
  | task_generator    | ✅     | 2        |
  | software_engineer | ✅     | 3        |
  | qa                | ✅     | 1        |
  | lessons_optimizer | ✅     | 1        |
```

If the workflow hits an escalation mid-run, the bridge posts to `#agent-escalations` as normal.
The trigger thread pauses, then continues streaming once the workflow resumes.

---

## Prerequisites

- **Python 3.10+** — all scripts are pure stdlib, no `pip install` needed
- **Claude Code** — the escalation hook is registered in `.claude/settings.json`
- **A Discord account** with permission to manage channels and bots
- **The "arigato-mr-roboto" Discord server** — create it or join via the project invite link

---

## Step 1 — Create a Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it `Agent Escalation Bot` → **Create**
3. In the left sidebar, click **Bot**
4. Click **Reset Token** → **Yes, do it** → **copy the token immediately** (you cannot see it again)
5. Under **Privileged Gateway Intents**, enable:
   - ✅ **Message Content Intent** — required to read message body text
6. Click **Save Changes**

> ⚠️ Treat the token like a password. Never commit it or log it.

---

## Step 2 — Set Up the Discord Server Channels

The system uses two channels. Both must exist before starting the bot.

| Channel | Purpose |
|---|---|
| `#agent-escalations` | Escalation notices posted here; threads created for `RESUME:` replies |
| `#trigger-agents` | You post `RUN: WI-XXXX` here to start a workflow |

### Creating the channels

1. Open your **arigato-mr-roboto** server in Discord
2. Hover over **Text Channels** in the sidebar → click the **+** icon
3. Select **Text Channel**, name it `agent-escalations`, click **Create Channel**
4. Repeat to create a text channel named `trigger-agents`

> Channel names are **case-sensitive** and must match `.claude/discord_config.json` exactly. Defaults are `agent-escalations` and `trigger-agents`.

### Required bot permissions (both channels)

After adding the bot in Step 3, verify it has these permissions in **each** channel:

| Permission | Why it's needed |
|---|---|
| View Channel | Reading messages |
| Send Messages | Posting escalations and acknowledgements |
| Create Public Threads | Creating per-escalation / per-run threads |
| Send Messages in Threads | Replying inside threads |
| Read Message History | Polling for new messages |

To verify: right-click the channel → **Edit Channel** → **Permissions** → find the bot's role.

---

## Step 3 — Add the Bot to Your Server

1. In the Developer Portal, go to **OAuth2 → URL Generator**
2. Under **Scopes**, check: `bot`
3. Under **Bot Permissions**, check all five permissions from the table above
4. Copy the **Generated URL** at the bottom of the page
5. Open the URL in your browser → select **arigato-mr-roboto** → **Authorise**

The bot now appears in the server member list.

---

## Step 4 — Configure the Repository

### `.claude/discord_config.json`

```json
{
  "allowed_user_ids": [],
  "guild_name": "arigato-mr-roboto",
  "channel_name": "agent-escalations",
  "trigger_channel_name": "trigger-agents",
  "notes": "Empty allowed_user_ids = any server member may send RESUME: or RUN: commands"
}
```

| Field | Description |
|---|---|
| `allowed_user_ids` | Discord user IDs allowed to send `RESUME:`. Empty = anyone |
| `guild_name` | Must match your Discord server name exactly |
| `channel_name` | Channel where escalation notices are posted |
| `trigger_channel_name` | Channel the trigger listener watches (informational only; not read by the bridge) |

**Finding your Discord user ID:** Settings → Advanced → enable **Developer Mode** → right-click your username → **Copy User ID**

### Environment variables (override `discord_config.json` at runtime)

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | _(required)_ | Bot token from the Developer Portal |
| `DISCORD_GUILD_NAME` | `arigato-mr-roboto` | Server name |
| `DISCORD_CHANNEL_NAME` | `agent-escalations` | Escalation channel |
| `DISCORD_ALLOWED_USER_IDS` | _(from config file)_ | Comma-separated user IDs |
| `DISCORD_POLL_SECONDS` | `5` | Escalation bridge poll interval |
| `DISCORD_TRIGGER_CHANNEL` | `trigger-agents` | Trigger listener channel |

---

## Step 5 — Set the Bot Token

```bash
export DISCORD_BOT_TOKEN=your_token_here
```

Persist it across sessions:

```bash
echo 'export DISCORD_BOT_TOKEN=your_token_here' >> ~/.zshrc
source ~/.zshrc
```

> Without `DISCORD_BOT_TOKEN`, the escalation bridge falls back to manual `resume.json` creation and the trigger listener refuses to start.

---

## Step 6 — Test the Connection

```bash
python3 .claude/scripts/test_discord_connection.py
```

Expected output:

```
1. Verifying bot identity...
   Bot: AgentEscalationBot#0  (id=1234567890123456789)
2. Looking for guild 'arigato-mr-roboto'...
   Guild ID: 1491462454843019304
3. Looking for channel '#agent-escalations'...
   Channel ID: 1491462455396794370
4. Posting test message...
   Message ID: 1234567890123456789
   Permalink:  https://discord.com/channels/...

✅ All checks passed — bridge is ready to use.
```

Check **#agent-escalations** in Discord — you should see a test message. If this step fails, see [Troubleshooting](#troubleshooting) before continuing.

---

## Step 7 — Start the Trigger Listener

The trigger listener is a persistent process. It must be running for `RUN:` commands in `#trigger-agents` to have any effect.

### Recommended command

```bash
DISCORD_BOT_TOKEN=<token> python3 .claude/scripts/discord_trigger_listener.py \
    --repo /Users/you/Code/mcs-products-mono-ui
```

### With an explicit runner script path

If the listener cannot find `run_headless.py` automatically (e.g. when run from a symlinked path), specify it directly:

```bash
DISCORD_BOT_TOKEN=<token> python3 .claude/scripts/discord_trigger_listener.py \
    --repo /Users/you/Code/mcs-products-mono-ui \
    --runner-script /Users/you/Code/mcs-products-mono-ui/agent-runner/run_headless.py
```

### With an explicit AI backend

```bash
DISCORD_BOT_TOKEN=<token> python3 .claude/scripts/discord_trigger_listener.py \
    --repo /Users/you/Code/mcs-products-mono-ui \
    --backend copilot
```

### Expected startup output

```
[trigger] 20:05:42 Default repo:  /Users/you/Code/mcs-products-mono-ui
[trigger] 20:05:42 Guild:         arigato-mr-roboto
[trigger] 20:05:42 Channel:       #trigger-agents
[trigger] 20:05:42 Poll interval: 10s
[trigger] 20:05:43 Runner script: /Users/you/Code/mcs-products-mono-ui/agent-runner/run_headless.py
[trigger] 20:05:43 Resolving Discord guild and channel…
[trigger] 20:05:43 Guild ID: 1491462454843019304
[trigger] 20:05:43 #trigger-agents channel ID: 1491495402690842816
[trigger] 20:05:44 Polling every 10s. Post 'RUN: WI-XXXX' in #trigger-agents to start a workflow.
[trigger] 20:05:44 Seeded last_seen_id=... (skipping existing history)
```

The listener runs until killed (`Ctrl-C` / `SIGTERM`).

### CLI arguments

| Argument | Default | Description |
|---|---|---|
| `--repo PATH` | git root of cwd | Default repo for triggered runs |
| `--backend copilot\|claude` | auto-detected | AI backend |
| `--runner-script PATH` | auto-resolved from `--repo` | Explicit path to `agent-runner/run_headless.py` |

### Run as a background service (macOS launchd)

Create `~/Library/LaunchAgents/com.mcs.discord-trigger-listener.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mcs.discord-trigger-listener</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/Code/mcs-products-mono-ui/.claude/scripts/discord_trigger_listener.py</string>
    <string>--repo</string>
    <string>/Users/you/Code/mcs-products-mono-ui</string>
    <string>--runner-script</string>
    <string>/Users/you/Code/mcs-products-mono-ui/agent-runner/run_headless.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DISCORD_BOT_TOKEN</key><string>YOUR_TOKEN_HERE</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/discord-trigger-listener.log</string>
  <key>StandardErrorPath</key><string>/tmp/discord-trigger-listener.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.mcs.discord-trigger-listener.plist
tail -f /tmp/discord-trigger-listener.log                 # watch output
launchctl unload ~/Library/LaunchAgents/com.mcs.discord-trigger-listener.plist  # stop
```

---

## Step 8 — Verify the Escalation Hook

The hook is pre-registered in `.claude/settings.json` — no changes needed. Confirm it is present:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command",
          "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/pause_on_escalate.py\"",
          "timeout": 10 }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command",
          "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/pause_on_escalate.py\"",
          "timeout": 10 }] }
    ]
  }
}
```

---

## Triggering a Workflow from Discord

Post in **#trigger-agents** while the listener is running:

### Basic

```
RUN: WI-4461550
```

### With an explicit repo path

```
RUN: WI-4461550 /Users/you/Code/mcs-products-mono-ui
```

The repo path overrides `--repo` for this run only.

### What happens after you post

1. Listener picks up the message within `DISCORD_POLL_SECONDS` (default 10s)
2. Thread `run-WI-4461550` created in `#trigger-agents`
3. Bot posts: *🤖 Workflow triggered by [your name] — starting…*
4. Live output streams as batched code blocks (every 20 lines or 30s)
5. Completion summary posted when the run finishes

### Behaviour reference

| Situation | What happens |
|---|---|
| Valid `RUN: WI-XXXX` | Workflow starts; thread created |
| Same change ID already running | Warning posted in active thread; new trigger skipped |
| `RUN:` with no change ID | Silently ignored |
| Workflow fails | ❌ summary with error message |
| Escalation mid-run | Bridge posts in `#agent-escalations`; trigger thread pauses until `RESUME:` |
| Existing intake artifacts found | Intake stage skipped automatically |
| HTTP 503 from Discord | Warning logged; listener retries next poll |

---

## Responding to Escalations in Discord

When an agent escalates, a message appears in **#agent-escalations** and a thread named `escalation-{CHANGE-ID}-{stage}` is created.

**Reply inside the thread** (not in `#agent-escalations` itself):

```
RESUME: Q1=Show a validation error toast, Q2=30 seconds
```

Other accepted formats:

```
RESUME:
Q1=Show a validation error toast
Q2=30 seconds
```

```
RESUME: Use 30 second timeouts and show an error toast on blur.
```

```
RESUME:
```

The bridge posts *"Resume acknowledged. Workflow will continue shortly."* and the workflow unblocks automatically. If you post without `RESUME:` at the start, the bot prompts you with the correct format.

---

## How Agents Raise Escalations

### `escalated.json` schema

Path: `{code_repo}/agent-context/{CHANGE-ID}/status/escalated.json`

```json
{
  "stage_key": "INTAKE",
  "reason": "Story has no clear acceptance criteria for AC-002",
  "blocking_questions": [
    "What should happen when the user submits an empty form?",
    "What is the timeout threshold for the API call?"
  ]
}
```

| Field | Required | Description |
|---|---|---|
| `stage_key` | ✅ | Current stage (`INTAKE`, `IMPL`, `QA`, etc.) |
| `reason` | ✅ | Why human input is needed |
| `blocking_questions` | ✅ | Ordered list — become `Q1`, `Q2` … in Discord |

### `resume.json` (written by the bridge, read by the agent)

```json
{
  "responder": "joshm2762",
  "timestamp": "2026-04-08T14:22:00Z",
  "answers": { "Q1": "Show a validation error toast", "Q2": "30 seconds" },
  "constraints": [],
  "extra_context": "RESUME: Q1=Show a validation error toast, Q2=30 seconds",
  "discord": { "guild_id": "...", "thread_id": "...", "permalink": "..." }
}
```

Agents read `answers.Q1`, `answers.Q2` etc. Fall back to `extra_context` for free-form replies.

---

## File Reference

| File | Purpose |
|---|---|
| `.claude/hooks/pause_on_escalate.py` | Claude Code hook — detects `escalated.json`, spawns bridge, blocks Stop |
| `.claude/scripts/discord_escalation_bridge.py` | Escalation bridge — posts to `#general`, polls for `RESUME:`, writes `resume.json` |
| `.claude/scripts/discord_trigger_listener.py` | Trigger listener — polls `#trigger-agents` for `RUN:`, launches `run_headless.py` |
| `.claude/scripts/test_discord_connection.py` | Smoke-test — verifies bot token, guild, channel access |
| `.claude/discord_config.json` | Bot config — allowed users, guild/channel names |
| `.claude/settings.json` | Claude Code hook registration |
| `agent-runner/run_headless.py` | Non-interactive workflow launcher used by the trigger listener |
| `agent-runner/run.py` | Core workflow runner (`run_workflow()` imported by `run_headless.py`) |
| `{CHANGE-ID}/status/escalated.json` | Written by agent to trigger an escalation |
| `{CHANGE-ID}/status/resume.json` | Written by bridge when human replies — unblocks the workflow |
| `{CHANGE-ID}/status/paused.json` | Written by hook at pause time |
| `{CHANGE-ID}/status/discord_bridge.pid` | Prevents duplicate bridge spawns |
| `{CHANGE-ID}/status/discord_bridge.log` | Bridge stdout/stderr |
| `{CHANGE-ID}/status/discord_notified.json` | Discord context saved after first post (allows bridge to reattach on restart) |
| `{CHANGE-ID}/status/escalated_archive/` | Archived `escalated.json` copies after each resumption |

---

## Troubleshooting

### Bot can't find the guild

```
Guild 'arigato-mr-roboto' not found. Bot is in: []
```

The bot has not been added to the server. Complete [Step 3](#step-3--add-the-bot-to-your-server).

---

### Bot can't find a channel

```
Channel #agent-escalations not found. Available text channels: ['general']
```

1. Create the missing channel (see [Step 2](#step-2--set-up-the-discord-server-channels))
2. Confirm the name in `.claude/discord_config.json` is exact (case-sensitive, no `#` prefix)
3. Confirm the bot role has **View Channels** + **Send Messages** in that channel

---

### `DISCORD_BOT_TOKEN not set`

The escalation bridge falls back to manual mode; the trigger listener refuses to start entirely. See [Step 5](#step-5--set-the-bot-token).

---

### Trigger listener can't find `run_headless.py`

```
ERROR: run_headless.py not found at /wrong/path/agent-runner/run_headless.py
  Pass --runner-script /path/to/agent-runner/run_headless.py to specify it explicitly.
```

Use `--runner-script` with the correct absolute path:

```bash
python3 .claude/scripts/discord_trigger_listener.py \
    --repo /Users/you/Code/mcs-products-mono-ui \
    --runner-script /Users/you/Code/mcs-products-mono-ui/agent-runner/run_headless.py
```

---

### Triggered run exits with code 2 immediately

The `Spawning:` log line shows the resolved script path. Verify it exists:

```bash
ls -la /path/shown/in/spawning/line
```

If it doesn't exist, pass `--runner-script` with the correct path.

---

### HTTP 503 from Discord (intermittent)

```
Warning: failed to fetch messages: ... HTTP 503: upstream connect error
```

Transient CDN error — the listener retries on the next poll automatically. Check [discordstatus.com](https://discordstatus.com) if it persists.

---

### Escalation bridge not spawning / stuck

```bash
cat agent-context/{CHANGE-ID}/status/discord_bridge.pid
tail -50 agent-context/{CHANGE-ID}/status/discord_bridge.log
```

To force a fresh spawn (stale PID file):

```bash
rm agent-context/{CHANGE-ID}/status/discord_bridge.pid
```

---

### `RESUME:` reply being ignored

1. Reply **inside the thread**, not in `#agent-escalations` directly
2. Message must start with `RESUME:` (case-insensitive)
3. If `allowed_user_ids` is non-empty in `discord_config.json`, confirm your user ID is listed
4. Check `discord_bridge.log` for details

---

### Workflow stuck — `resume.json` exists but hasn't unblocked

The hook only fires on a Claude Code `Stop`/`SubagentStop` event. Trigger any agent action to produce a Stop event and the hook will detect `resume.json` automatically.

---

### End-to-end escalation dry-run (no real workflow needed)

```bash
mkdir -p /tmp/test_status
cat > /tmp/test_escalated.json << 'EOF'
{
  "stage_key": "TEST",
  "reason": "Dry-run test",
  "blocking_questions": ["Is the system working?"]
}
EOF

python3 .claude/scripts/discord_escalation_bridge.py \
  --escalated-path /tmp/test_escalated.json \
  --status-dir /tmp/test_status \
  --change-id TEST-001 \
  --dry-run
```

In a second terminal, simulate the Discord reply:

```bash
echo "RESUME: Q1=Yes, it works!" > /tmp/test_status/discord_simulated_message.txt
```

The bridge parses the reply and writes `/tmp/test_status/resume.json`.
